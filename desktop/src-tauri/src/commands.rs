//! Команды трея и окна (04-DESKTOP §8.1).
//!
//! Полный список команд десктопа собран в `lib.rs::run()` (`invoke_handler`),
//! но объявлена каждая команда рядом со своей реализацией — иначе `#[tauri::command]`
//! генерирует одноимённые макросы и крейт просто не собирается:
//!
//! | Команда | Где реализована |
//! |---|---|
//! | `set_badge`, `toggle_main`, `set_presence` | этот файл (§3) |
//! | `open_external`, `app_info` | этот файл (мост `PlatformBridge`, 03 §7) |
//! | `notify_show`, `notify_reply` | [`crate::notify`] (§4) |
//! | `set_session_token`, `set_api_base` | [`crate::cache`] (§5.5) |
//! | `dpapi_encrypt`, `dpapi_decrypt` | [`crate::cache::dpapi`] (§5.5) |
//! | `cache_*` | [`crate::cache::store`] (§5.2) |
//! | `outbox_*` | [`crate::cache::outbox`] (§5.3) |
//! | `sync_*` | [`crate::sync`] (§5.4) |
//! | `updater_*` | [`crate::updater`] (§6) |
//!
//! **Соглашение об именах.** Tauri 2 по умолчанию ждёт аргументы в camelCase;
//! команды с многословными аргументами переключены на `rename_all = "snake_case"`,
//! чтобы имена совпадали с REST-контрактом (01-API-SPEC) и с полями структур:
//! `invoke("outbox_push", { dialog_id, kind, body })`.

use serde::Serialize;
use tauri::AppHandle;
use tauri_plugin_opener::OpenerExt;

/// Бейдж непрочитанных: иконка трея, тултип, пункт «Новые чаты: N», оверлей таскбара.
///
/// Источник счётчика — фронт: на каждое изменение суммы непрочитанных он зовёт
/// команду с debounce 300 мс (04 §3.2). В офлайне сумма берётся из кэша.
#[tauri::command]
pub fn set_badge(app: AppHandle, count: u32) -> Result<(), String> {
    crate::tray::set_badge(&app, count).map_err(|e| e.to_string())
}

/// Показать/спрятать главное окно — то же, что Ctrl+Shift+L и ЛКМ по значку трея.
#[tauri::command]
pub fn toggle_main(app: AppHandle) -> Result<(), String> {
    crate::tray::toggle_main(&app);
    Ok(())
}

/// Статус сменили в UI → переставить галки в меню трея и обновить тултип.
///
/// Обратное направление — событие `presence:set` из трея: фронт по нему делает
/// `PUT /api/v1/presence` (01 §11.6) и подтверждает результат этой командой.
/// Rust в API не ходит.
#[tauri::command]
pub fn set_presence(app: AppHandle, status: String) -> Result<(), String> {
    crate::tray::set_presence(&app, &status)
}

/// Открыть ссылку в системном браузере — «Открыть на Авито ↗» (03 §7).
///
/// Без этой команды `target="_blank"` открылся бы внутри WebView2. Схема
/// проверяется намеренно: ссылка приходит из данных диалога (внешний источник),
/// а `open_url` на Windows зовёт `ShellExecute` — то есть любой протокол
/// зарегистрированного обработчика. Разрешаем только http(s).
#[tauri::command]
pub fn open_external(app: AppHandle, url: String) -> Result<(), String> {
    if !is_external_http_url(&url) {
        return Err(format!(
            "open_external: разрешены только http(s)-ссылки: {url}"
        ));
    }
    app.opener()
        .open_url(url, None::<&str>)
        .map_err(|e| e.to_string())
}

fn is_external_http_url(url: &str) -> bool {
    let rest = match url.split_once("://") {
        Some((scheme, rest)) => {
            let scheme = scheme.to_ascii_lowercase();
            if scheme != "http" && scheme != "https" {
                return false;
            }
            rest
        }
        None => return false,
    };
    // Пустой хост («https://») и управляющие символы (перевод строки в аргументе
    // ShellExecute) — не ссылка, а попытка что-то подсунуть.
    !rest.is_empty() && !rest.starts_with('/') && !url.chars().any(|c| c.is_control() || c == '"')
}

/// Версия и канал сборки для экрана «О программе» (04 §8.1, опциональная команда).
#[derive(Debug, Clone, Serialize)]
pub struct AppInfo {
    pub version: String,
    pub channel: String,
}

/// Источник истины версии — `tauri.conf.json` (её же CI сверяет с тегом).
#[tauri::command]
pub fn app_info(app: AppHandle) -> AppInfo {
    AppInfo {
        version: app.package_info().version.to_string(),
        channel: "stable".to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::is_external_http_url;

    #[test]
    fn accepts_normal_links() {
        assert!(is_external_http_url("https://www.avito.ru/items/123"));
        assert!(is_external_http_url("HTTP://example.com"));
    }

    #[test]
    fn rejects_non_http_schemes_and_garbage() {
        assert!(!is_external_http_url("file:///C:/Windows/system32/cmd.exe"));
        assert!(!is_external_http_url("javascript:alert(1)"));
        assert!(!is_external_http_url("ms-settings:privacy"));
        assert!(!is_external_http_url("C:\\Windows\\system32\\cmd.exe"));
        assert!(!is_external_http_url("https://"));
        assert!(!is_external_http_url("https:///etc/passwd"));
        assert!(!is_external_http_url("https://example.com\n& calc.exe"));
    }
}
