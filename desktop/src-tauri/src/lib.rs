//! LeadChat desktop — сборка приложения Tauri 2 (04-DESKTOP §1.4).
//!
//! Десктоп — **обёртка над тем же React-бандлом**, что и веб (DESIGN 1.7):
//! никакой второй логики продукта здесь нет. Rust отвечает ровно за то, чего
//! у веба быть не может: трей и бейдж, тосты Windows, офлайн-кэш и outbox,
//! автозапуск, глобальный хоткей, автообновление, deep-link `leadchat://`.
//!
//! Модули:
//! * [`tray`] — иконка, меню, бейдж непрочитанных (§3);
//! * [`commands`] — команды трея и окна (§8);
//! * [`notify`] + [`toast_activator`] — тосты Windows и их активация (§4);
//! * [`cache`] — DPAPI-ключ, шифрованный SQLite, outbox (§5);
//! * [`sync`] — синхронизация кэша и таймер flush (§5.4);
//! * [`updater`] — расписание проверок обновлений (§6).
//!
//! Этот файл — единственное место, где всё связывается: плагины, `setup`,
//! `on_window_event` и `invoke_handler`. Команды объявлены рядом со своей
//! реализацией, а регистрируются одним списком здесь.
//!
//! Ограничение среды: разработка идёт на macOS, поэтому весь Windows-код —
//! под `#[cfg(target_os = "windows")]`, а `cargo check` обязан проходить на macOS.

pub mod cache;
pub mod commands;
pub mod notify;
pub mod sync;
pub mod toast_activator;
pub mod tray;
pub mod updater;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use tauri::{AppHandle, Emitter, Listener, Manager, WindowEvent};
use tauri_plugin_autostart::MacosLauncher;
use tauri_plugin_notification::NotificationExt;

/// Метка главного окна (совпадает с `app.windows[0].label` в tauri.conf.json).
pub const MAIN_WINDOW: &str = "main";

/// Схема deep-link, зарегистрированная в Windows (04 §2).
pub const DEEP_LINK_SCHEME: &str = "leadchat://";

/// Аргумент автозапуска: стартуем сразу в трей, окно не показываем (04 §2).
pub const MINIMIZED_ARG: &str = "--minimized";

/// Rust → фронт: перейти на маршрут SPA. Полезная нагрузка — строка вида `/chats/{id}`
/// (тот же формат, что у тостов, 04 §4.2).
pub const EVENT_NAVIGATE: &str = "navigate";

/// Rust → фронт: пользователь сменил статус в меню трея. Значения — `online` | `away`
/// (01-API-SPEC §11.6). Фронт делает `PUT /api/v1/presence`; Rust в API не ходит.
pub const EVENT_PRESENCE_SET: &str = "presence:set";

/// Фронт → Rust: слушатели навешаны, можно отдавать отложенный deep-link
/// (холодный старт по клику в тосте — окно ещё не успело подписаться на события).
pub const EVENT_APP_READY: &str = "app:ready";

/// Разделы SPA, в которые разрешено вести deep-link. Ссылку `leadchat://…` может
/// открыть любой сайт, поэтому маршрут не берём «как есть» — только из белого списка.
const DEEP_LINK_ROOTS: &[&str] = &["chats", "settings", "stats", "team", "templates"];

/// Одноразовые флаги приложения (переживают только процесс; на диске — маркер-файл).
#[derive(Default)]
pub struct AppFlags {
    /// Тост «работаем в трее» показывается один раз за установку.
    close_hint_shown: AtomicBool,
}

/// Отложенная навигация: deep-link, пришедший до готовности фронта.
#[derive(Default)]
pub struct PendingNavigate {
    route: Mutex<Option<String>>,
    /// Фронт уже прислал `app:ready` — копить маршруты больше не нужно.
    ready: AtomicBool,
}

/// Имя маркер-файла для одноразового тоста про трей (04 §3.3).
/// В доке флаг живёт в таблице `meta` кэша; здесь — отдельный файл, чтобы скелет
/// не зависел от модуля кэша: подсказка должна показаться и тогда, когда кэш не
/// поднялся (сломанный `cache.db`, чужой профиль Windows).
const CLOSE_HINT_MARKER: &str = "close-to-tray-hint.done";

/// Точка входа (вызывается из `main.rs`).
pub fn run() {
    tauri::Builder::default()
        // 1. single-instance — строго ПЕРВЫМ плагином: вторая копия должна отвалиться
        //    до того, как успеет проинициализировать что-либо ещё (04 §1.4).
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            tray::show_main(app);
            // deep-link второй копии приходит аргументом командной строки.
            if let Some(url) = argv.iter().find(|a| a.starts_with(DEEP_LINK_SCHEME)) {
                handle_deep_link(app, url);
            }
        }))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent, // на Windows игнорируется
            Some(vec![MINIMIZED_ARG]),  // автозапуск → сразу в трей
        ))
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(
            tauri_plugin_sql::Builder::default()
                .add_migrations("sqlite:cache.db", sql_migrations())
                .build(),
        )
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_opener::init())
        .manage(AppFlags::default())
        .manage(PendingNavigate::default())
        .setup(|app| {
            let handle = app.handle().clone();

            // Кэш поднимаем первым: от него зависят outbox, синхронизация и быстрый
            // ответ из тоста. Падение НЕ фатально — приложение обязано открыться и в
            // онлайне; иначе сломанный cache.db превращается в «не запускается вообще».
            if let Err(err) = cache::init(&handle) {
                log::error!("кэш не поднялся, офлайн-режим недоступен: {err}");
            }
            if let Err(err) = notify::init(&handle) {
                log::warn!("уведомления: инициализация не удалась: {err}");
            }
            if let Err(err) = sync::init(&handle) {
                log::warn!("синхронизация: таймер не поднялся: {err}");
            }
            if let Err(err) = updater::init(&handle) {
                log::warn!("автообновление: расписание не поднялось: {err}");
            }

            tray::init(&handle)?;
            setup_deep_link(&handle);
            setup_global_shortcut(&handle)?;

            // Запуск из автостарта (`--minimized`) — окно не показываем, живём в трее.
            if std::env::args().any(|a| a == MINIMIZED_ARG) {
                if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
                    let _ = window.hide();
                }
            }

            // Холодный старт по deep-link: аргумент есть, а фронт ещё не слушает —
            // кладём маршрут в очередь и отдаём по «app:ready».
            if let Some(url) = std::env::args()
                .skip(1)
                .find(|a| a.starts_with(DEEP_LINK_SCHEME))
            {
                queue_navigate(&handle, &url);
            }
            let ready_handle = handle.clone();
            app.listen(EVENT_APP_READY, move |_| {
                flush_pending_navigate(&ready_handle)
            });

            Ok(())
        })
        // Крестик/Alt+F4 = свернуть в трей: процесс живёт, WS и тосты работают (04 §3.3).
        // Реально завершает приложение только пункт «Выход» в трее (`app.exit(0)`).
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == MAIN_WINDOW {
                    api.prevent_close();
                    let _ = window.hide();
                    show_close_hint_once(window.app_handle());
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            // трей и окно (§3)
            commands::set_badge,
            commands::toggle_main,
            commands::set_presence,
            commands::open_external,
            commands::app_info,
            // уведомления (§4)
            notify::notify_show,
            notify::notify_reply,
            // сессия и кэш (§5)
            cache::set_session_token,
            cache::set_api_base,
            cache::dpapi::dpapi_encrypt,
            cache::dpapi::dpapi_decrypt,
            cache::store::cache_load_dialogs,
            cache::store::cache_load_messages,
            cache::store::cache_apply_sync,
            cache::store::cache_upsert_messages,
            cache::store::cache_unread_total,
            cache::store::cache_clear,
            // исходящая очередь (§5.3)
            cache::outbox::outbox_push,
            cache::outbox::outbox_flush,
            cache::outbox::outbox_list,
            cache::outbox::outbox_retry,
            cache::outbox::outbox_delete,
            // синхронизация (§5.4)
            sync::sync_startup,
            sync::sync_reconnect,
            sync::sync_dialog_messages,
            sync::sync_last_at,
            // автообновление (§6)
            updater::updater_check,
            updater::updater_install,
            updater::updater_snooze,
            updater::updater_status,
        ])
        .run(tauri::generate_context!())
        .expect("LeadChat: не удалось запустить приложение");
}

/// Миграции кэша для `tauri-plugin-sql`.
///
/// Основной путь применения — rusqlite внутри `CacheState::open` (модуль кэша не
/// зависит от плагина). Здесь тот же список отдаётся плагину, чтобы схема совпадала,
/// если БД когда-нибудь откроют его API; SQL идемпотентен (`CREATE TABLE IF NOT EXISTS`),
/// поэтому повторное применение безопасно.
fn sql_migrations() -> Vec<tauri_plugin_sql::Migration> {
    cache::migrations()
        .into_iter()
        .map(|m| tauri_plugin_sql::Migration {
            version: m.version,
            description: m.description,
            sql: m.sql,
            kind: tauri_plugin_sql::MigrationKind::Up,
        })
        .collect()
}

// ---------------------------------------------------------------------------
// deep-link: leadchat://chats/{id} → событие `navigate` с маршрутом /chats/{id}
// ---------------------------------------------------------------------------

fn setup_deep_link(app: &AppHandle) {
    // В dev-сборке схема не прописана инсталлятором — регистрируем её на лету.
    // API есть только на Windows и Linux, поэтому под cfg (на macOS не компилируется).
    #[cfg(any(windows, target_os = "linux"))]
    {
        use tauri_plugin_deep_link::DeepLinkExt;
        if let Err(err) = app.deep_link().register_all() {
            log::warn!("deep-link: не удалось зарегистрировать схему: {err}");
        }
    }

    let handle = app.clone();
    {
        use tauri_plugin_deep_link::DeepLinkExt;
        app.deep_link().on_open_url(move |event| {
            for url in event.urls() {
                handle_deep_link(&handle, url.as_str());
            }
        });
    }
}

/// Единая обработка `leadchat://…`, откуда бы ссылка ни пришла: клик по тосту,
/// вторая копия приложения (single-instance), внешняя ссылка.
///
/// Разбор активации (в т.ч. `?reply=1` — быстрый ответ в outbox) живёт в
/// `notify::on_toast_activated`, сюда он попадает через `toast_activator`
/// (04 §4.2): один путь для protocol- и COM-активации, без дублей.
pub fn handle_deep_link(app: &AppHandle, url: &str) {
    if parse_deep_link(url).is_none() {
        log::warn!("deep-link: ссылка отклонена: {url}");
        return;
    }
    tray::show_main(app);
    toast_activator::handle_protocol_activation(app, url);
    // Страховка для холодного старта: если фронт ещё не подписан, маршрут уйдёт
    // повторно по «app:ready» (навигация идемпотентна).
    queue_navigate(app, url);
}

/// Разобрать `leadchat://chats/{id}` в маршрут SPA `/chats/{id}`.
///
/// Возвращает `None`, если ссылка не наша, ведёт в неизвестный раздел или содержит
/// подозрительные сегменты: deep-link приходит извне, доверять ему нельзя.
pub fn parse_deep_link(raw: &str) -> Option<String> {
    let rest = raw.strip_prefix(DEEP_LINK_SCHEME)?;
    let path = rest.split(['?', '#']).next().unwrap_or_default();
    let segments: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();

    let root = match segments.first() {
        None => return Some("/chats".to_string()), // просто «развернуть список»
        Some(root) => *root,
    };
    if !DEEP_LINK_ROOTS.contains(&root) {
        return None;
    }
    let safe = segments.iter().all(|s| {
        s.len() <= 64
            && *s != ".."
            && s.chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))
    });
    if !safe {
        return None;
    }
    Some(format!("/{}", segments.join("/")))
}

/// Запомнить маршрут, пока фронт не подписался на события. После `app:ready`
/// ничего не копим: событие уже дошло до слушателей, повтор был бы «прыжком»
/// в другой диалог после обычной перезагрузки окна.
fn queue_navigate(app: &AppHandle, url: &str) {
    let Some(pending) = app.try_state::<PendingNavigate>() else {
        return;
    };
    if pending.ready.load(Ordering::SeqCst) {
        return;
    }
    if let Some(mut route) = parse_deep_link(url) {
        // Сохраняем признак «фокус в композер» — тот же формат маршрута, что у
        // notify::focus_conversation, чтобы фронт разбирал ровно один вариант.
        if url.contains("reply=1") && route.starts_with("/chats/") {
            route.push_str("?reply=1");
        }
        if let Ok(mut slot) = pending.route.lock() {
            *slot = Some(route);
        }
    }
}

fn flush_pending_navigate(app: &AppHandle) {
    let Some(pending) = app.try_state::<PendingNavigate>() else {
        return;
    };
    pending.ready.store(true, Ordering::SeqCst);
    let route = pending.route.lock().ok().and_then(|mut slot| slot.take());
    if let Some(route) = route {
        let _ = app.emit(EVENT_NAVIGATE, route);
    }
}

// ---------------------------------------------------------------------------
// глобальный хоткей Ctrl+Shift+L (10-UX §5.1, 04 §2)
// ---------------------------------------------------------------------------

fn setup_global_shortcut(app: &AppHandle) -> tauri::Result<()> {
    use tauri_plugin_global_shortcut::{
        Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState,
    };

    let toggle = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::KeyL);
    let handle = app.clone();
    let result = app
        .global_shortcut()
        .on_shortcut(toggle, move |_app, _shortcut, event| {
            // Реагируем только на нажатие: иначе toggle сработает дважды.
            if event.state() == ShortcutState::Pressed {
                tray::toggle_main(&handle);
            }
        });
    if let Err(err) = result {
        // Хоткей мог занять другой процесс — это не повод не запускаться.
        log::warn!("global-shortcut: Ctrl+Shift+L недоступен: {err}");
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// одноразовый тост «работаем в трее» (04 §3.3)
// ---------------------------------------------------------------------------

fn show_close_hint_once(app: &AppHandle) {
    let Some(flags) = app.try_state::<AppFlags>() else {
        return;
    };
    if flags.close_hint_shown.swap(true, Ordering::SeqCst) {
        return;
    }

    let marker = match app.path().app_data_dir() {
        Ok(dir) => dir.join(CLOSE_HINT_MARKER),
        Err(err) => {
            log::warn!("не удалось определить app_data_dir: {err}");
            return;
        }
    };
    if marker.exists() {
        return;
    }
    if let Some(parent) = marker.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let _ = std::fs::write(&marker, b"shown");

    let _ = app
        .notification()
        .builder()
        .title("LeadChat продолжает работать в трее")
        .body("Сообщения продолжают приходить. Значок в трее — развернуть, «Выход» — закрыть приложение.")
        .show();
}

#[cfg(test)]
mod tests {
    use super::parse_deep_link;

    #[test]
    fn parses_conversation_link() {
        assert_eq!(
            parse_deep_link("leadchat://chats/018f3c2a-9b1e-7c4d-a5f6-0e1d2c3b4a59"),
            Some("/chats/018f3c2a-9b1e-7c4d-a5f6-0e1d2c3b4a59".to_string())
        );
    }

    #[test]
    fn bare_link_opens_list() {
        assert_eq!(parse_deep_link("leadchat://"), Some("/chats".to_string()));
        assert_eq!(
            parse_deep_link("leadchat://chats"),
            Some("/chats".to_string())
        );
    }

    #[test]
    fn query_and_trailing_slash_are_ignored() {
        assert_eq!(
            parse_deep_link("leadchat://chats/42/?from=toast#x"),
            Some("/chats/42".to_string())
        );
    }

    #[test]
    fn rejects_foreign_and_unsafe_links() {
        assert_eq!(parse_deep_link("https://example.com/chats/1"), None);
        assert_eq!(parse_deep_link("leadchat://evil/1"), None);
        assert_eq!(parse_deep_link("leadchat://chats/../../etc/passwd"), None);
        assert_eq!(parse_deep_link("leadchat://chats/<script>"), None);
    }
}
