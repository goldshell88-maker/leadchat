//! Уведомления Windows: матрица показа, троттлинг и интерактивный тост.
//!
//! Источник истины — `docs/04-DESKTOP.md` §4 (матрица §4.1, XML тоста §4.2,
//! троттлинг §4.3) и `docs/11-SCREENS.md` §7.2 (что видит сотрудник).
//!
//! Ключевые решения этого модуля:
//!
//! * **Решение «показывать ли тост» принимает Rust.** Состояние окна (скрыто /
//!   свёрнуто / не в фокусе) читается из Tauri, а не приходит из JS: фронт может
//!   передать подсказку `foreground`, но по умолчанию она не нужна.
//! * **Троттлинг живёт в памяти процесса** (`static NOTIFY_STATE`): перезапуск
//!   приложения обнуляет счётчики — это осознанно, хранить их негде и незачем.
//! * **Весь WinRT-код — под `#[cfg(target_os = "windows")]`.** На macOS/Linux
//!   собирается заглушка, которая пишет в лог «тост показан бы: …» вместе с
//!   готовым XML — вся логика решения и рендера при этом выполняется по-настоящему
//!   и покрыта юнит-тестами, так что разработка на macOS полноценна.
//! * **Быстрый ответ из тоста идёт через outbox** (04 §5.3), а не прямым HTTP:
//!   ответ, набранный без сети, не теряется.
//!
//! Активация тоста (какая именно кнопка нажата и что делать) — `toast_activator.rs`.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::{Mutex, MutexGuard, OnceLock};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager};

// ---------------------------------------------------------------------------
// Константы
// ---------------------------------------------------------------------------

/// AppUserModelID приложения. Должен совпадать с `identifier` из `tauri.conf.json`
/// и с AUMID ярлыка в меню «Пуск», который создаёт NSIS-инсталлятор, — иначе
/// Windows молча не покажет тост Win32-приложения.
pub const AUMID: &str = "ru.partner-lead-centre.leadchat";

/// Группа тостов в Центре уведомлений: все наши тосты схлопываются в один блок.
pub const TOAST_GROUP: &str = "leadchat";

/// Тег сводного тоста: сводка всегда обновляет саму себя, а не копится стопкой.
pub const SUMMARY_TAG: &str = "summary";

/// Схема deep-link'а (регистрируется `tauri-plugin-deep-link`, 04 §2).
pub const DEEP_LINK_SCHEME: &str = "leadchat";

/// Не чаще одного тоста на диалог в 30 с (04 §4.3).
pub const PER_DIALOG_COOLDOWN: Duration = Duration::from_secs(30);
/// Окно глобального лимита.
pub const GLOBAL_WINDOW: Duration = Duration::from_secs(10);
/// Не более 3 тостов за `GLOBAL_WINDOW`, дальше — сводный режим.
pub const GLOBAL_MAX: usize = 3;
/// В сводном режиме — не чаще одного сводного тоста в минуту.
pub const SUMMARY_EVERY: Duration = Duration::from_secs(60);
/// 30 с тишины — выход из сводного режима.
pub const QUIET_RESUME: Duration = Duration::from_secs(30);
/// Первая сводка после входа в сводный режим показывается не мгновенно, а через
/// эту паузу — чтобы успеть накопить осмысленное «N сообщений в M диалогах»
/// вместо «1 сообщение в 1 диалоге» сразу за третьим обычным тостом.
pub const SUMMARY_FIRST_DELAY: Duration = Duration::from_secs(2);
/// Шаг фонового таймера сводного режима.
const SUMMARY_TICK: Duration = Duration::from_secs(1);

/// Ограничения длины: Windows всё равно обрежет, но лучше обрезать самим и по
/// границе символа (в теле — кириллица, байтовая обрезка ломает UTF-8).
const TITLE_MAX_CHARS: usize = 80;
const BODY_MAX_CHARS: usize = 160;

fn log(msg: &str) {
    eprintln!("[leadchat::notify] {msg}");
}

// ---------------------------------------------------------------------------
// Типы запроса/ответа
// ---------------------------------------------------------------------------

/// Тип события, из-за которого фронт просит тост.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NotifyKind {
    /// Новое сообщение в диалоге (WS `message:new`).
    #[default]
    Message,
    /// «Диалог передан вам» (WS `conversation:assigned` с `is_for_you`, 01 §11.5).
    Assigned,
    /// Явный запрос сводного тоста (обычно сводку строит сам Rust).
    Summary,
    /// Служебное: `needs_reauth` аккаунта Авито и т.п. (04 §4.1, последняя строка).
    System,
}

/// Запрос на показ тоста. Поля принимаются и в camelCase (как их естественно
/// шлёт JS), и в snake_case (как в примерах 04 §8.1) — фронт и документ
/// расходятся в стиле, ломаться из-за этого нельзя.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct NotifyRequest {
    #[serde(alias = "conversation_id")]
    pub conversation_id: String,
    /// «Иван Петров · Ремонт iPhone 13».
    #[serde(default)]
    pub title: String,
    /// Текст сообщения.
    #[serde(default)]
    pub body: String,
    /// «LP-Москва» — аккаунт-получатель, строка attribution.
    #[serde(default)]
    pub attribution: String,
    #[serde(default)]
    pub kind: NotifyKind,
    /// `in | out | note | system` из `messages.direction`.
    #[serde(default)]
    pub direction: Option<String>,
    /// `client | operator | bot | system` из `messages.sender_type`.
    #[serde(default, alias = "sender_type")]
    pub sender_type: Option<String>,
    /// Для `kind = assigned`: назначили именно текущему пользователю.
    #[serde(default, alias = "is_for_you")]
    pub is_for_you: Option<bool>,
    /// У ролей head/observer кнопки «Ответить» в тосте нет (11 §7.2).
    #[serde(default, alias = "can_reply")]
    pub can_reply: Option<bool>,
    /// Статус «отошёл» и последующие тосты серии — без звука.
    #[serde(default)]
    pub silent: Option<bool>,
    /// Подсказка о состоянии окна. `None` — Rust определит сам (обычный случай).
    #[serde(default)]
    pub foreground: Option<bool>,
}

/// Совместимость с именем типа из 04 §8.1.
pub type ToastRequest = NotifyRequest;

/// Что в итоге произошло с запросом — возвращается фронту, чтобы он мог
/// не играть свой звук поверх системного и понимать, копится ли сводка.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct NotifyOutcome {
    /// Тост реально показан.
    pub shown: bool,
    /// `toast` | `summary` | `suppressed`.
    pub mode: &'static str,
    /// Машинно-читаемая причина решения (см. `Decision`).
    pub reason: &'static str,
    /// Показан без звука (или подавлен).
    pub silent: bool,
    /// Копится в сводке на данный момент.
    pub pending_messages: u32,
    pub pending_dialogs: u32,
    /// Приложение сейчас в сводном режиме.
    pub summary_mode: bool,
}

/// Решение матрицы + троттлинга. Отдельный тип, чтобы логику можно было
/// тестировать без Tauri и без Windows.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    /// Показать обычный тост. `priority` — событие handoff (вне троттлинга).
    Toast { silent: bool, priority: bool },
    /// Показать сводный тост.
    Summary {
        messages: u32,
        dialogs: u32,
        silent: bool,
    },
    /// Не показывать; строка — причина (уходит во фронт и в лог).
    Skip(&'static str),
}

// ---------------------------------------------------------------------------
// Состояние троттлинга (в памяти процесса)
// ---------------------------------------------------------------------------

#[derive(Debug, Default)]
pub struct NotifyState {
    /// Когда последний раз показывали тост по каждому диалогу.
    per_dialog_last: HashMap<String, Instant>,
    /// Моменты последних показов (для лимита «3 за 10 с»).
    recent: VecDeque<Instant>,
    /// Включён сводный режим.
    summary_mode: bool,
    summary_mode_since: Option<Instant>,
    /// Последнее входящее событие (для `QUIET_RESUME`).
    last_event_at: Option<Instant>,
    /// Последняя показанная сводка.
    last_summary_at: Option<Instant>,
    /// Накопленное для сводки.
    pending_messages: u32,
    pending_dialogs: HashSet<String>,
    /// Фоновый таймер сводного режима уже запущен.
    summary_timer_armed: bool,
}

static NOTIFY_STATE: OnceLock<Mutex<NotifyState>> = OnceLock::new();

fn state() -> &'static Mutex<NotifyState> {
    NOTIFY_STATE.get_or_init(|| Mutex::new(NotifyState::default()))
}

/// Отравленный mutex здесь не повод падать: троттлинг — не критичные данные.
fn lock_state() -> MutexGuard<'static, NotifyState> {
    state()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// Сбросить троттлинг (логаут, смена пользователя, тесты).
pub fn reset_throttle() {
    let mut st = lock_state();
    *st = NotifyState::default();
}

// ---------------------------------------------------------------------------
// Инициализация
// ---------------------------------------------------------------------------

/// Вызывается из `lib.rs::setup()`: регистрирует активатор тостов (или сообщает,
/// что работает protocol-фолбэк) и обнуляет состояние.
pub fn init(app: &AppHandle) -> Result<(), String> {
    reset_throttle();
    crate::toast_activator::init(app)?;
    log(&format!(
        "инициализирован; AUMID={AUMID}, активация тостов: {}",
        crate::toast_activator::mode().describe()
    ));
    Ok(())
}

// ---------------------------------------------------------------------------
// Команды Tauri
// ---------------------------------------------------------------------------

/// Показать тост по матрице 04 §4.1 с троттлингом 04 §4.3.
///
/// Фронт вызывает её на каждое подходящее WS-событие и не обязан сам считать
/// окна и лимиты: решение принимается здесь, фронт лишь узнаёт из ответа,
/// показан ли тост (чтобы не дублировать звук — 10 §5.4 п.4).
#[tauri::command]
pub fn notify_show(app: AppHandle, req: NotifyRequest) -> Result<NotifyOutcome, String> {
    let now = Instant::now();
    let foreground = req.foreground.unwrap_or_else(|| window_foreground(&app));

    let decision = {
        let mut st = lock_state();
        decide(&mut st, &req, foreground, now)
    };

    let result = match &decision {
        Decision::Toast { silent, priority } => {
            let xml = build_message_xml(&req, *silent, *priority);
            show_toast_xml(&xml, &toast_tag(&req.conversation_id))?;
            outcome(
                true,
                "toast",
                if *priority { "priority" } else { "matrix" },
                *silent,
            )
        }
        Decision::Summary {
            messages,
            dialogs,
            silent,
        } => {
            let xml = build_summary_xml(*messages, *dialogs, *silent);
            show_toast_xml(&xml, SUMMARY_TAG)?;
            outcome(true, "summary", "summary", *silent)
        }
        Decision::Skip(reason) => outcome(false, "suppressed", reason, true),
    };

    // Пока идёт поток — держим фоновый таймер, иначе последняя сводка зависнет
    // до следующего события (а поток может закончиться в любой момент).
    if lock_state().summary_mode {
        arm_summary_timer(&app);
    }
    Ok(result)
}

/// Быстрый ответ из тоста.
///
/// Всегда через outbox (04 §5.3): строка ложится в локальную очередь, UI сразу
/// рисует ⏳, отправку делает `outbox_flush` — поэтому ответ, набранный при
/// выдернутой сети, не теряется. Возвращает `client_msg_id` (он же
/// `client_message_id` для идемпотентности, 01 §1.6).
#[tauri::command]
pub fn notify_reply(
    app: AppHandle,
    conversation_id: String,
    text: String,
) -> Result<String, String> {
    reply_via_outbox(&app, &conversation_id, &text)
}

fn outcome(shown: bool, mode: &'static str, reason: &'static str, silent: bool) -> NotifyOutcome {
    let st = lock_state();
    NotifyOutcome {
        shown,
        mode,
        reason,
        silent,
        pending_messages: st.pending_messages,
        pending_dialogs: st.pending_dialogs.len() as u32,
        summary_mode: st.summary_mode,
    }
}

// ---------------------------------------------------------------------------
// Обработка активации тоста
// ---------------------------------------------------------------------------

/// Единая точка входа для активации тоста — вызывается и COM-активатором
/// (`toast_activator.rs`, background-активация), и обработчиком deep-link'а
/// (`deeplink.rs`, protocol-активация «Открыть» / клик по телу тоста).
///
/// `args` — либо `action=reply&conv={id}` (кнопка), либо `leadchat://chats/{id}`
/// (протокол); оба формата разбираются.
pub fn on_toast_activated(app: &AppHandle, args: &str, user_input: Option<&str>) {
    let conv = parse_arg(args, "conv")
        .or_else(|| conversation_from_launch(args))
        .unwrap_or_default();

    // Кнопка «Ответить»: в COM-режиме — action=reply, в protocol-фолбэке — ?reply=1.
    let wants_reply = parse_arg(args, "action").as_deref() == Some("reply")
        || parse_arg(args, "reply").as_deref() == Some("1");

    if wants_reply {
        match user_input.map(str::trim).filter(|t| !t.is_empty()) {
            Some(text) => match reply_via_outbox(app, &conv, text) {
                Ok(_) => {}
                Err(err) => {
                    log(&format!("быстрый ответ не поставлен в очередь: {err}"));
                    // Текст не теряем для человека: открываем диалог, можно добить руками.
                    focus_conversation(app, &conv, true);
                }
            },
            // Пустое поле ввода (или фолбэк без поля) — просто открыть диалог
            // с фокусом в композере (04 §4.2, 11 §7.2).
            None => focus_conversation(app, &conv, true),
        }
    } else {
        focus_conversation(app, &conv, false);
    }
}

/// Поставить быстрый ответ в исходящую очередь.
fn reply_via_outbox(app: &AppHandle, conversation_id: &str, text: &str) -> Result<String, String> {
    let conv = conversation_id.trim();
    if conv.is_empty() {
        return Err("не указан диалог для быстрого ответа".into());
    }
    let text = text.trim();
    if text.is_empty() {
        return Err("пустой текст быстрого ответа".into());
    }

    // Очередь — зона кэша (04 §5.3). Вариант `*_from_app` существует ровно для
    // этого случая: под рукой только AppHandle (клик по тосту, не команда).
    let client_msg_id = crate::cache::outbox::push_from_app(app, conv, "message", text)?;

    let _ = app.emit(
        "outbox:queued",
        serde_json::json!({
            "client_msg_id": client_msg_id,
            "conversation_id": conv,
            "source": "toast",
        }),
    );
    spawn_outbox_flush(app);
    log(&format!("быстрый ответ поставлен в очередь: диалог {conv}"));
    Ok(client_msg_id)
}

/// Прогнать очередь, не блокируя вызывающий поток (клик по тосту приходит из
/// COM-потока Windows — там ждать сеть нельзя).
fn spawn_outbox_flush(app: &AppHandle) {
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        if let Err(err) = crate::cache::outbox::flush_from_app(handle).await {
            log(&format!(
                "outbox_flush после быстрого ответа не удался: {err}"
            ));
        }
    });
}

/// Развернуть окно и открыть нужный диалог.
///
/// `focus_composer` — поставить курсор в поле ответа: так ведёт себя кнопка
/// «Ответить» в protocol-фолбэке (11 §7.2). Фронт читает `?reply=1` в маршруте.
fn focus_conversation(app: &AppHandle, conversation_id: &str, focus_composer: bool) {
    focus_main(app);
    let conv = sanitize_id(conversation_id);
    let route = match (conv.is_empty(), focus_composer) {
        (true, _) => "/chats".to_string(),
        (false, false) => format!("/chats/{conv}"),
        (false, true) => format!("/chats/{conv}?reply=1"),
    };
    let _ = app.emit("navigate", route);
}

/// Показать главное окно. Намеренно не зовём `tray::show_main`, чтобы
/// уведомления не зависели от модуля трея (зоны разные, поведение тривиальное).
fn focus_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

/// Окно «на переднем плане» = видимо, не свёрнуто и в фокусе. Любое другое
/// состояние по матрице 04 §4.1 означает «тост показываем».
fn window_foreground(app: &AppHandle) -> bool {
    match app.get_webview_window("main") {
        Some(window) => {
            window.is_visible().unwrap_or(false)
                && !window.is_minimized().unwrap_or(false)
                && window.is_focused().unwrap_or(false)
        }
        // Окна нет (стартуем/закрываемся) — считаем, что пользователь не смотрит.
        None => false,
    }
}

// ---------------------------------------------------------------------------
// Разбор аргументов активации
// ---------------------------------------------------------------------------

/// Достать значение из `k=v&k2=v2` (в т.ч. из query-части URL).
pub fn parse_arg(args: &str, key: &str) -> Option<String> {
    let query = match args.split_once('?') {
        Some((_, tail)) => tail,
        None => args,
    };
    for pair in query.split('&') {
        if let Some((k, v)) = pair.split_once('=') {
            if k.trim() == key {
                return Some(percent_decode(v));
            }
        }
    }
    None
}

/// Достать id диалога из `leadchat://chats/{id}` (клик по телу тоста).
pub fn conversation_from_launch(args: &str) -> Option<String> {
    let rest = args.strip_prefix(&format!("{DEEP_LINK_SCHEME}://"))?;
    let path = rest.split('?').next().unwrap_or(rest);
    let mut parts = path.split('/').filter(|s| !s.is_empty());
    if parts.next()? != "chats" {
        return None;
    }
    parts.next().map(|id| id.to_string())
}

/// Минимальный percent-decode: Windows отдаёт аргументы как есть, но deep-link
/// приходит URL-кодированным.
fn percent_decode(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or("");
                match u8::from_str_radix(hex, 16) {
                    Ok(byte) => {
                        out.push(byte);
                        i += 3;
                    }
                    Err(_) => {
                        out.push(bytes[i]);
                        i += 1;
                    }
                }
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            other => {
                out.push(other);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

// ---------------------------------------------------------------------------
// Матрица показа + троттлинг (04 §4.1, §4.3)
// ---------------------------------------------------------------------------

/// Чистое решение: состояние + запрос + состояние окна + «сейчас» → что делать.
/// Вынесено из команды, чтобы тестировать без Tauri (см. `mod tests`).
pub fn decide(
    st: &mut NotifyState,
    req: &NotifyRequest,
    foreground: bool,
    now: Instant,
) -> Decision {
    match req.kind {
        // ⚑ «Диалог передан вам» — тост всегда, даже в фокусе, и вне троттлинга
        // (04 §4.1 и §4.3, последняя строка).
        NotifyKind::Assigned => {
            if req.is_for_you == Some(false) {
                return Decision::Skip("assigned_not_for_you");
            }
            st.last_event_at = Some(now);
            Decision::Toast {
                silent: req.silent.unwrap_or(false),
                priority: true,
            }
        }

        // Служебные тосты (needs_reauth и т.п.) — без матрицы и троттлинга,
        // их частота задаётся источником события.
        NotifyKind::System => Decision::Toast {
            silent: req.silent.unwrap_or(false),
            priority: false,
        },

        // Явная сводка по запросу фронта.
        NotifyKind::Summary => {
            let messages = st.pending_messages.max(1);
            let dialogs = (st.pending_dialogs.len() as u32).max(1);
            st.pending_messages = 0;
            st.pending_dialogs.clear();
            st.last_summary_at = Some(now);
            Decision::Summary {
                messages,
                dialogs,
                silent: true,
            }
        }

        NotifyKind::Message => decide_message(st, req, foreground, now),
    }
}

fn decide_message(
    st: &mut NotifyState,
    req: &NotifyRequest,
    foreground: bool,
    now: Instant,
) -> Decision {
    // Только входящие от клиента. Сообщения бота, коллег и эхо своих — молча
    // (04 §4.1; 10 §5.4 п.1). Поля не заданы — считаем, что фронт уже отфильтровал.
    if let Some(sender) = req.sender_type.as_deref() {
        if sender != "client" {
            return Decision::Skip(match sender {
                "bot" => "sender_bot",
                "operator" => "sender_operator",
                _ => "sender_not_client",
            });
        }
    } else {
        // Предупреждаем один раз за запуск: в потоке сообщений лог не засоряем.
        static WARNED: std::sync::Once = std::sync::Once::new();
        WARNED.call_once(|| log("senderType не передан — фильтр «только от клиента» не проверен"));
    }
    if let Some(direction) = req.direction.as_deref() {
        if direction != "in" {
            return Decision::Skip("direction_not_in");
        }
    }

    // Окно на переднем плане — тоста нет ни при открытом, ни при закрытом диалоге:
    // человек и так видит список и слышит звук (04 §4.1, строки 1–2).
    if foreground {
        return Decision::Skip("window_foreground");
    }

    prune_recent(st, now);

    // Выход из сводного режима после тишины.
    if st.summary_mode {
        let quiet = st
            .last_event_at
            .is_none_or(|t| now.saturating_duration_since(t) >= QUIET_RESUME);
        if quiet {
            leave_summary_mode(st);
        }
    }
    st.last_event_at = Some(now);

    if st.summary_mode {
        accumulate(st, &req.conversation_id);
        return if summary_due(st, now) {
            take_summary(st, now)
        } else {
            Decision::Skip("summary_mode")
        };
    }

    // Не чаще одного тоста на диалог в 30 с; подавленное всё равно попадёт в сводку.
    if let Some(last) = st.per_dialog_last.get(&req.conversation_id) {
        if now.saturating_duration_since(*last) < PER_DIALOG_COOLDOWN {
            accumulate(st, &req.conversation_id);
            return Decision::Skip("dialog_cooldown");
        }
    }

    // Больше трёх тостов за 10 с — переходим в сводный режим. Первую сводку
    // отдаёт фоновый таймер через `SUMMARY_FIRST_DELAY`, чтобы она была
    // содержательной.
    if st.recent.len() >= GLOBAL_MAX {
        enter_summary_mode(st, now);
        accumulate(st, &req.conversation_id);
        return Decision::Skip("summary_mode_entered");
    }

    // Звук — только у первого тоста серии (04 §4.3): если в окне уже был показ,
    // остальные молчат.
    let silent = req.silent.unwrap_or(false) || !st.recent.is_empty();
    st.recent.push_back(now);
    st.per_dialog_last.insert(req.conversation_id.clone(), now);
    Decision::Toast {
        silent,
        priority: false,
    }
}

fn prune_recent(st: &mut NotifyState, now: Instant) {
    while let Some(front) = st.recent.front() {
        if now.saturating_duration_since(*front) >= GLOBAL_WINDOW {
            st.recent.pop_front();
        } else {
            break;
        }
    }
}

fn accumulate(st: &mut NotifyState, conversation_id: &str) {
    st.pending_messages = st.pending_messages.saturating_add(1);
    st.pending_dialogs.insert(conversation_id.to_string());
}

fn enter_summary_mode(st: &mut NotifyState, now: Instant) {
    if !st.summary_mode {
        st.summary_mode = true;
        st.summary_mode_since = Some(now);
        st.last_summary_at = None;
        log("поток сообщений: переходим в сводный режим");
    }
}

fn leave_summary_mode(st: &mut NotifyState) {
    st.summary_mode = false;
    st.summary_mode_since = None;
    st.last_summary_at = None;
    st.pending_messages = 0;
    st.pending_dialogs.clear();
    st.recent.clear();
    log("тишина: выходим из сводного режима");
}

/// Пора ли показывать сводку: первую — через `SUMMARY_FIRST_DELAY` после входа
/// в режим, последующие — не чаще `SUMMARY_EVERY`.
fn summary_due(st: &NotifyState, now: Instant) -> bool {
    if st.pending_messages == 0 {
        return false;
    }
    match st.last_summary_at {
        Some(last) => now.saturating_duration_since(last) >= SUMMARY_EVERY,
        None => st
            .summary_mode_since
            .is_none_or(|since| now.saturating_duration_since(since) >= SUMMARY_FIRST_DELAY),
    }
}

fn take_summary(st: &mut NotifyState, now: Instant) -> Decision {
    let messages = st.pending_messages;
    let dialogs = st.pending_dialogs.len() as u32;
    st.pending_messages = 0;
    st.pending_dialogs.clear();
    st.last_summary_at = Some(now);
    Decision::Summary {
        messages,
        dialogs,
        // Сводка — продолжение серии, звук уже был у первого тоста.
        silent: true,
    }
}

/// Фоновый таймер сводного режима: пока идёт поток, раз в секунду проверяет,
/// не пора ли отдать сводку, и гасит режим после `QUIET_RESUME` тишины.
/// Обычный поток (`std::thread`) — работы на микросекунды, тащить сюда tokio незачем.
fn arm_summary_timer(app: &AppHandle) {
    {
        let mut st = lock_state();
        if st.summary_timer_armed || !st.summary_mode {
            return;
        }
        st.summary_timer_armed = true;
    }

    let app = app.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(SUMMARY_TICK);
        let now = Instant::now();

        let (summary, stop) = {
            let mut st = lock_state();
            if !st.summary_mode {
                st.summary_timer_armed = false;
                (None, true)
            } else {
                let quiet = st
                    .last_event_at
                    .is_none_or(|t| now.saturating_duration_since(t) >= QUIET_RESUME);
                // Перед выходом из режима сводку добиваем, чтобы «хвост» потока
                // не остался непоказанным.
                let summary = if summary_due(&st, now) || (quiet && st.pending_messages > 0) {
                    Some(take_summary(&mut st, now))
                } else {
                    None
                };
                if quiet {
                    leave_summary_mode(&mut st);
                    st.summary_timer_armed = false;
                    (summary, true)
                } else {
                    (summary, false)
                }
            }
        };

        if let Some(Decision::Summary {
            messages,
            dialogs,
            silent,
        }) = summary
        {
            // Показ — строго на главном потоке: у свежего std-потока нет
            // COM-апартамента, и WinRT-вызов вернул бы CO_E_NOTINITIALIZED.
            // Синхронные команды Tauri уже идут на главном потоке, поэтому
            // отдельный apartment нам нигде больше не нужен.
            let emitter = app.clone();
            let dispatch = app.run_on_main_thread(move || {
                let xml = build_summary_xml(messages, dialogs, silent);
                if let Err(err) = show_toast_xml(&xml, SUMMARY_TAG) {
                    log(&format!("сводный тост не показан: {err}"));
                }
                let _ = emitter.emit(
                    "notify:summary",
                    serde_json::json!({ "messages": messages, "dialogs": dialogs }),
                );
            });
            if let Err(err) = dispatch {
                log(&format!(
                    "сводку не удалось отправить в главный поток: {err}"
                ));
            }
        }
        if stop {
            break;
        }
    });
}

// ---------------------------------------------------------------------------
// Рендер XML тоста (04 §4.2)
// ---------------------------------------------------------------------------

/// Тег тоста = id диалога: новое сообщение того же диалога заменяет свой тост
/// в Центре уведомлений, а не плодит стопку (04 §4.2).
fn toast_tag(conversation_id: &str) -> String {
    let id = sanitize_id(conversation_id);
    // Ограничение WinRT на длину тега — 64 символа; uuid укладывается.
    id.chars().take(64).collect()
}

/// В id диалога с сервера приходит uuid; всё остальное в URL/тег не пускаем.
fn sanitize_id(raw: &str) -> String {
    raw.chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_')
        .collect()
}

fn xml_escape(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len() + 8);
    for ch in raw.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&apos;"),
            // Управляющие символы XML 1.0 не переваривает — режем.
            c if (c as u32) < 0x20 && c != '\n' && c != '\t' => out.push(' '),
            c => out.push(c),
        }
    }
    out
}

fn truncate(raw: &str, max_chars: usize) -> String {
    let trimmed = raw.trim();
    if trimmed.chars().count() <= max_chars {
        return trimmed.to_string();
    }
    let mut out: String = trimmed.chars().take(max_chars.saturating_sub(1)).collect();
    out.push('…');
    out
}

/// XML тоста о новом сообщении.
///
/// Кнопки зависят от того, поднят ли COM-активатор:
/// * `ComCallback` — «Ответить» с инлайн-полем (background-активация);
/// * `Protocol` (фолбэк 04 §4.2) — «Ответить» открывает диалог с фокусом в
///   композере (`?reply=1`), поля ввода нет: без COM-активатора Windows
///   физически некуда отдать набранный текст.
pub fn build_message_xml(req: &NotifyRequest, silent: bool, priority: bool) -> String {
    let conv = sanitize_id(&req.conversation_id);
    // Служебные тосты (04 §4.1, needs_reauth) диалога не имеют — открываем список.
    let launch = if conv.is_empty() {
        format!("{DEEP_LINK_SCHEME}://chats")
    } else {
        format!("{DEEP_LINK_SCHEME}://chats/{conv}")
    };
    // Кнопка «Ответить» бессмысленна без диалога.
    let can_reply = req.can_reply.unwrap_or(true) && !conv.is_empty();
    let com_activation = crate::toast_activator::mode().is_com_callback();

    let title = truncate(&req.title, TITLE_MAX_CHARS);
    let body = truncate(&req.body, BODY_MAX_CHARS);
    let attribution = truncate(&req.attribution, TITLE_MAX_CHARS);

    let mut xml = String::with_capacity(1024);
    // Клик по телу тоста = «Открыть»: protocol-активация поднимает/фокусирует
    // приложение, single-instance пробрасывает URL в живой процесс (04 §2).
    xml.push_str(&format!(
        "<toast launch=\"{}\" activationType=\"protocol\">",
        xml_escape(&launch)
    ));
    xml.push_str("<visual><binding template=\"ToastGeneric\">");
    if priority {
        // ⚑ из 10 §4: отличает передачу диалога от обычного сообщения.
        xml.push_str(&format!("<text>⚑ {}</text>", xml_escape(&title)));
    } else {
        xml.push_str(&format!("<text>{}</text>", xml_escape(&title)));
    }
    xml.push_str(&format!("<text>{}</text>", xml_escape(&body)));
    if !attribution.is_empty() {
        xml.push_str(&format!(
            "<text placement=\"attribution\">{}</text>",
            xml_escape(&attribution)
        ));
    }
    xml.push_str("</binding></visual>");

    xml.push_str("<actions>");
    if can_reply {
        if com_activation {
            xml.push_str(
                "<input id=\"replyText\" type=\"text\" placeHolderContent=\"Быстрый ответ…\"/>",
            );
            xml.push_str(&format!(
                "<action content=\"Ответить\" activationType=\"background\" \
                 arguments=\"{}\" hint-inputId=\"replyText\"/>",
                xml_escape(&format!("action=reply&conv={conv}"))
            ));
        } else {
            xml.push_str(&format!(
                "<action content=\"Ответить\" activationType=\"protocol\" arguments=\"{}\"/>",
                xml_escape(&format!("{launch}?reply=1"))
            ));
        }
    }
    xml.push_str(&format!(
        "<action content=\"Открыть\" activationType=\"protocol\" arguments=\"{}\"/>",
        xml_escape(&launch)
    ));
    xml.push_str("</actions>");

    xml.push_str(if silent {
        "<audio silent=\"true\"/>"
    } else {
        "<audio src=\"ms-winsoundevent:Notification.IM\"/>"
    });
    xml.push_str("</toast>");
    xml
}

/// XML сводного тоста: «12 новых сообщений в 5 диалогах» (04 §4.3).
pub fn build_summary_xml(messages: u32, dialogs: u32, silent: bool) -> String {
    let launch = format!("{DEEP_LINK_SCHEME}://chats");
    let text = summary_text(messages, dialogs);
    format!(
        "<toast launch=\"{launch}\" activationType=\"protocol\">\
         <visual><binding template=\"ToastGeneric\">\
         <text>LeadChat</text><text>{text}</text>\
         </binding></visual>\
         <actions><action content=\"Открыть LeadChat\" activationType=\"protocol\" \
         arguments=\"{launch}\"/></actions>\
         {audio}</toast>",
        launch = xml_escape(&launch),
        text = xml_escape(&text),
        audio = if silent {
            "<audio silent=\"true\"/>"
        } else {
            "<audio src=\"ms-winsoundevent:Notification.IM\"/>"
        }
    )
}

pub fn summary_text(messages: u32, dialogs: u32) -> String {
    format!(
        "{messages} {new} {msg} в {dialogs} {dlg}",
        new = plural(messages, ["новое", "новых", "новых"]),
        msg = plural(messages, ["сообщение", "сообщения", "сообщений"]),
        dlg = plural(dialogs, ["диалоге", "диалогах", "диалогах"]),
    )
}

/// Русские числительные: 1 сообщение / 2 сообщения / 5 сообщений.
fn plural(n: u32, forms: [&str; 3]) -> &str {
    let hundreds = n % 100;
    let tens = n % 10;
    if (11..=14).contains(&hundreds) {
        forms[2]
    } else if tens == 1 {
        forms[0]
    } else if (2..=4).contains(&tens) {
        forms[1]
    } else {
        forms[2]
    }
}

// ---------------------------------------------------------------------------
// Показ тоста: WinRT на Windows, лог-заглушка на остальных платформах
// ---------------------------------------------------------------------------

/// Отдать готовый XML в Windows Action Center.
#[cfg(target_os = "windows")]
pub fn show_toast_xml(xml: &str, tag: &str) -> Result<(), String> {
    // CHECK: сигнатуры под windows 0.61 (features Data_Xml_Dom, UI_Notifications
    // уже есть в Cargo.toml). WinRT-часть между 0.58 и 0.61 не менялась, но при
    // подъёме версии crate — сверить ещё раз на живой Windows.
    use windows::core::HSTRING;
    use windows::Data::Xml::Dom::XmlDocument;
    use windows::UI::Notifications::{ToastNotification, ToastNotificationManager};

    let document = XmlDocument::new().map_err(|e| format!("XmlDocument::new: {e}"))?;
    document
        .LoadXml(&HSTRING::from(xml))
        .map_err(|e| format!("LoadXml: {e}"))?;

    let toast = ToastNotification::CreateToastNotification(&document)
        .map_err(|e| format!("CreateToastNotification: {e}"))?;
    toast
        .SetTag(&HSTRING::from(tag))
        .map_err(|e| format!("SetTag: {e}"))?;
    toast
        .SetGroup(&HSTRING::from(TOAST_GROUP))
        .map_err(|e| format!("SetGroup: {e}"))?;

    // Win32-приложению нужен именно notifier по AUMID: без ярлыка с этим AUMID
    // в меню «Пуск» Windows тост не покажет (04 §4.2).
    let notifier = ToastNotificationManager::CreateToastNotifierWithId(&HSTRING::from(AUMID))
        .map_err(|e| format!("CreateToastNotifierWithId({AUMID}): {e}"))?;
    notifier.Show(&toast).map_err(|e| format!("Show: {e}"))
}

/// Заглушка для разработки на macOS/Linux: тост не показывается, но всё
/// решение и весь XML — настоящие, их видно в консоли `tauri dev`.
#[cfg(not(target_os = "windows"))]
pub fn show_toast_xml(xml: &str, tag: &str) -> Result<(), String> {
    log(&format!(
        "тост показан бы: tag={tag}, group={TOAST_GROUP}\n{xml}"
    ));
    Ok(())
}

// ---------------------------------------------------------------------------
// Тесты (выполняются на macOS — вся логика решения платформенно-независима)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn msg(conv: &str) -> NotifyRequest {
        NotifyRequest {
            conversation_id: conv.to_string(),
            title: "Иван Петров · Ремонт iPhone 13".into(),
            body: "А сколько будет стоить замена экрана?".into(),
            attribution: "LP-Москва".into(),
            kind: NotifyKind::Message,
            direction: Some("in".into()),
            sender_type: Some("client".into()),
            ..Default::default()
        }
    }

    fn shown(d: &Decision) -> bool {
        matches!(d, Decision::Toast { .. } | Decision::Summary { .. })
    }

    #[test]
    fn bot_messages_are_never_toasted() {
        let mut st = NotifyState::default();
        let mut req = msg("c1");
        req.sender_type = Some("bot".into());
        assert_eq!(
            decide(&mut st, &req, false, Instant::now()),
            Decision::Skip("sender_bot")
        );
    }

    #[test]
    fn outgoing_and_notes_are_never_toasted() {
        let mut st = NotifyState::default();
        let mut req = msg("c1");
        req.direction = Some("note".into());
        req.sender_type = Some("operator".into());
        assert!(!shown(&decide(&mut st, &req, false, Instant::now())));
    }

    #[test]
    fn foreground_window_suppresses_message_toast() {
        let mut st = NotifyState::default();
        assert_eq!(
            decide(&mut st, &msg("c1"), true, Instant::now()),
            Decision::Skip("window_foreground")
        );
    }

    #[test]
    fn handoff_is_shown_even_in_foreground_and_ignores_throttle() {
        let mut st = NotifyState::default();
        let now = Instant::now();
        // Забиваем глобальный лимит.
        for i in 0..GLOBAL_MAX {
            let _ = decide(&mut st, &msg(&format!("c{i}")), false, now);
        }
        let assigned = NotifyRequest {
            conversation_id: "c9".into(),
            title: "Диалог передан вам".into(),
            kind: NotifyKind::Assigned,
            is_for_you: Some(true),
            ..Default::default()
        };
        assert_eq!(
            decide(&mut st, &assigned, true, now),
            Decision::Toast {
                silent: false,
                priority: true
            }
        );
    }

    #[test]
    fn per_dialog_cooldown_is_30s() {
        let mut st = NotifyState::default();
        let t0 = Instant::now();
        assert!(shown(&decide(&mut st, &msg("c1"), false, t0)));
        assert_eq!(
            decide(&mut st, &msg("c1"), false, t0 + Duration::from_secs(29)),
            Decision::Skip("dialog_cooldown")
        );
        // Через 31 с — можно снова (глобальное окно к тому моменту пустое).
        assert!(shown(&decide(
            &mut st,
            &msg("c1"),
            false,
            t0 + Duration::from_secs(31)
        )));
    }

    #[test]
    fn only_first_toast_of_a_series_has_sound() {
        let mut st = NotifyState::default();
        let t0 = Instant::now();
        assert_eq!(
            decide(&mut st, &msg("c1"), false, t0),
            Decision::Toast {
                silent: false,
                priority: false
            }
        );
        assert_eq!(
            decide(&mut st, &msg("c2"), false, t0 + Duration::from_secs(1)),
            Decision::Toast {
                silent: true,
                priority: false
            }
        );
    }

    #[test]
    fn away_status_shows_toast_without_sound() {
        let mut st = NotifyState::default();
        let mut req = msg("c1");
        req.silent = Some(true);
        assert_eq!(
            decide(&mut st, &req, false, Instant::now()),
            Decision::Toast {
                silent: true,
                priority: false
            }
        );
    }

    /// Приёмка 04 §10: «при потоке 20 сообщений/10 с — не более 3 тостов + сводный».
    #[test]
    fn burst_of_20_messages_yields_three_toasts_and_a_summary() {
        let mut st = NotifyState::default();
        let t0 = Instant::now();
        let mut toasts = 0;
        let mut summaries = 0;

        for i in 0..20u32 {
            // 20 сообщений в 5 диалогах за 10 секунд.
            let conv = format!("c{}", i % 5);
            let now = t0 + Duration::from_millis(u64::from(i) * 500);
            match decide(&mut st, &msg(&conv), false, now) {
                Decision::Toast { .. } => toasts += 1,
                Decision::Summary { .. } => summaries += 1,
                Decision::Skip(_) => {}
            }
        }

        assert_eq!(toasts, GLOBAL_MAX, "обычных тостов должно быть ровно 3");
        assert!(summaries >= 1, "в потоке должна появиться сводка");
        assert!(
            toasts + summaries <= 5,
            "всего тостов за поток: {}",
            toasts + summaries
        );
        assert!(st.summary_mode, "после потока остаёмся в сводном режиме");
    }

    #[test]
    fn summary_counts_suppressed_messages() {
        let mut st = NotifyState::default();
        let t0 = Instant::now();
        for i in 0..12u32 {
            let conv = format!("c{}", i % 5);
            let now = t0 + Duration::from_millis(u64::from(i) * 100);
            let _ = decide(&mut st, &msg(&conv), false, now);
        }
        // Первая сводка выходит не раньше SUMMARY_FIRST_DELAY после входа в режим.
        let decision = decide(
            &mut st,
            &msg("c7"),
            false,
            t0 + SUMMARY_FIRST_DELAY + Duration::from_secs(1),
        );
        match decision {
            Decision::Summary {
                messages,
                dialogs,
                silent,
            } => {
                assert!(messages >= 5, "сводка учитывает подавленные: {messages}");
                assert!(dialogs >= 2, "и число диалогов: {dialogs}");
                assert!(silent, "сводка не звучит — звук был у первого тоста серии");
            }
            other => panic!("ожидалась сводка, получено {other:?}"),
        }
    }

    #[test]
    fn quiet_period_resumes_normal_toasts() {
        let mut st = NotifyState::default();
        let t0 = Instant::now();
        for i in 0..10u32 {
            let now = t0 + Duration::from_millis(u64::from(i) * 200);
            let _ = decide(&mut st, &msg(&format!("c{i}")), false, now);
        }
        assert!(st.summary_mode);

        let after_quiet = t0 + QUIET_RESUME + Duration::from_secs(5);
        assert_eq!(
            decide(&mut st, &msg("c-new"), false, after_quiet),
            Decision::Toast {
                silent: false,
                priority: false
            },
            "после 30 с тишины возвращаемся к обычным тостам со звуком"
        );
    }

    #[test]
    fn russian_plurals() {
        assert_eq!(summary_text(1, 1), "1 новое сообщение в 1 диалоге");
        assert_eq!(summary_text(2, 3), "2 новых сообщения в 3 диалогах");
        assert_eq!(summary_text(12, 5), "12 новых сообщений в 5 диалогах");
        assert_eq!(summary_text(21, 11), "21 новое сообщение в 11 диалогах");
        assert_eq!(summary_text(114, 22), "114 новых сообщений в 22 диалогах");
    }

    #[test]
    fn xml_is_escaped_and_well_formed() {
        let mut req = msg("c-1<script>");
        req.title = "Иван & Ко <b>".into();
        req.body = "цена \"под ключ\" < 5000 & скидка".into();
        let xml = build_message_xml(&req, false, false);

        assert!(!xml.contains("<script>"), "id диалога санитизируется");
        assert!(xml.contains("&amp;"));
        assert!(xml.contains("&lt;b&gt;"));
        assert!(xml.contains("leadchat://chats/c-1script"));
        assert!(xml.starts_with("<toast "));
        assert!(xml.ends_with("</toast>"));
    }

    #[test]
    fn fallback_toast_has_no_input_field() {
        // На macOS активатор всегда в protocol-режиме — это же и есть фолбэк 04 §4.2.
        let xml = build_message_xml(&msg("c1"), false, false);
        assert!(!xml.contains("<input"), "без COM-активатора поля ввода нет");
        assert!(xml.contains("content=\"Ответить\""));
        assert!(xml.contains("?reply=1"));
        assert!(xml.contains("content=\"Открыть\""));
    }

    #[test]
    fn head_and_observer_get_toast_without_reply_button() {
        let mut req = msg("c1");
        req.can_reply = Some(false);
        let xml = build_message_xml(&req, true, false);
        assert!(!xml.contains("Ответить"));
        assert!(xml.contains("Открыть"));
        assert!(xml.contains("<audio silent=\"true\"/>"));
    }

    #[test]
    fn long_body_is_truncated_on_char_boundary() {
        let mut req = msg("c1");
        req.body = "я".repeat(400);
        let xml = build_message_xml(&req, false, false);
        assert!(xml.contains('…'));
        assert!(xml.chars().count() < 1200);
    }

    #[test]
    fn activation_args_are_parsed() {
        assert_eq!(
            parse_arg("action=reply&conv=abc-123", "conv").as_deref(),
            Some("abc-123")
        );
        assert_eq!(
            parse_arg("action=reply&conv=abc-123", "action").as_deref(),
            Some("reply")
        );
        assert_eq!(parse_arg("action=reply", "conv"), None);
        assert_eq!(
            conversation_from_launch("leadchat://chats/abc-123?reply=1").as_deref(),
            Some("abc-123")
        );
        assert_eq!(conversation_from_launch("leadchat://chats"), None);
        assert_eq!(
            parse_arg("leadchat://chats/x?reply=1", "reply").as_deref(),
            Some("1")
        );
        assert_eq!(
            percent_decode("%D0%9F%D1%80%D0%B8%D0%B2%D0%B5%D1%82+%21"),
            "Привет !"
        );
    }

    #[test]
    fn system_toast_without_conversation_opens_chat_list() {
        let req = NotifyRequest {
            conversation_id: String::new(),
            title: "Аккаунт Авито требует переподключения".into(),
            body: "LP-Москва — обновите доступ в настройках".into(),
            kind: NotifyKind::System,
            ..Default::default()
        };
        let mut st = NotifyState::default();
        // Служебный тост показывается независимо от окна и троттлинга.
        assert!(shown(&decide(&mut st, &req, true, Instant::now())));

        let xml = build_message_xml(&req, false, false);
        assert!(xml.contains("launch=\"leadchat://chats\""));
        assert!(!xml.contains("chats/\""), "не должно быть висящего слэша");
        assert!(!xml.contains("Ответить"), "без диалога отвечать некуда");
    }

    #[test]
    fn summary_xml_targets_chat_list() {
        let xml = build_summary_xml(12, 5, true);
        assert!(xml.contains("12 новых сообщений в 5 диалогах"));
        assert!(xml.contains("leadchat://chats"));
        assert!(!xml.contains("<input"));
        assert!(xml.contains("<audio silent=\"true\"/>"));
    }
}
