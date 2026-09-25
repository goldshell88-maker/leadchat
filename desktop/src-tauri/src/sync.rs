//! Синхронизация кэша с сервером (04-DESKTOP §5.4).
//!
//! ```text
//! Старт приложения
//!   1. cache_load_dialogs()        → UI отрисован из SQLite мгновенно, офлайн-бейдж
//!   2. GET /conversations?limit=200 → cache_apply_sync(): upsert, СЕРВЕР ПРАВ, prune 200
//!   3. для открытого диалога: GET /conversations/{id}/messages?limit=100 → upsert
//!   4. outbox_flush()
//!   5. meta.last_sync_at = серверная метка
//!
//! Reconnect WS
//!   1. GET /conversations?updated_since={last_sync_at}&limit=200 → дельта
//!   2. догрузка сообщений открытого диалога
//!   3. outbox_flush()
//! ```
//!
//! Плюс фоновый таймер: раз в 30 с прогоняем очередь, если в ней что-то есть.

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};

use crate::cache::{
    format_iso, now_epoch, now_iso, outbox, parse_iso, store, CacheState, DialogSync, MessageSync,
};

/// Сколько диалогов тянем за раз (01 §5.1 — фиксированная серверная сортировка даёт топ-200).
pub const CONV_LIMIT: u32 = 200;
/// Сколько сообщений тянем на диалог (04 §5.1).
pub const MSG_LIMIT: u32 = 100;
/// Период фонового flush'а очереди (04 §5.3).
pub const FLUSH_INTERVAL_SECS: u64 = 30;
/// Перекрытие курсора дельты — на случай расхождения часов (01 §11.7).
pub const CURSOR_OVERLAP_SECS: i64 = 30;
/// Офлайн дольше этого — дельта не окупается, грузим список целиком (01 §11.7 п.4).
pub const FULL_RESYNC_AFTER_SECS: i64 = 30 * 60;

/// Ключ курсора синхронизации в таблице `meta`.
pub const META_LAST_SYNC: &str = "last_sync_at";
/// Событие «кэш обновился» — UI перечитывает снапшот.
pub const EVENT_SYNCED: &str = "cache:synced";

/// Итог одного прохода синхронизации.
#[derive(Debug, Clone, Default, Serialize)]
pub struct SyncReport {
    /// Полная синхронизация (а не дельта по `updated_since`).
    pub full: bool,
    /// Сколько диалогов записано в кэш.
    pub dialogs: u32,
    /// Сколько сообщений записано в кэш.
    pub messages: u32,
    /// Новый курсор (`meta.last_sync_at`).
    pub last_sync_at: Option<String>,
    /// Сеть недоступна — работаем на кэше.
    pub offline: bool,
    /// Итог попутного прохода очереди.
    pub flush: Option<outbox::FlushReport>,
}

// ---------------------------------------------------------------------------
// HTTP
// ---------------------------------------------------------------------------

/// GET с access-JWT. На `401` эмитит `session:refresh-needed` (фронт делает refresh
/// по httpOnly-cookie и присылает новый токен через `set_session_token`).
///
/// События уходят через [`outbox::FlushEvents`] — тот же канал, что у очереди;
/// благодаря этому синхронизацию можно прогонять в тестах без WebView.
async fn get_json(
    events: &dyn outbox::FlushEvents,
    state: &CacheState,
    url: &str,
) -> Result<serde_json::Value, String> {
    let token = state.session_token().map_err(|_| {
        events.session_refresh_needed();
        "no_session_token".to_string()
    })?;

    let resp = state
        .http()
        .get(url)
        .bearer_auth(&token)
        .send()
        .await
        .map_err(|e| format!("network: {e}"))?;

    let status = resp.status().as_u16();
    if status == 401 {
        events.session_refresh_needed();
        return Err("unauthorized".to_string());
    }
    if !resp.status().is_success() {
        return Err(format!("HTTP {status}"));
    }
    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("bad json: {e}"))
}

/// `GET /api/v1/conversations` (опционально с `updated_since`).
pub async fn fetch_conversations(
    events: &dyn outbox::FlushEvents,
    state: &CacheState,
    updated_since: Option<&str>,
) -> Result<Vec<DialogSync>, String> {
    let mut url = format!(
        "{}/api/v1/conversations?limit={CONV_LIMIT}",
        state.api_base()
    );
    if let Some(since) = updated_since {
        url.push_str("&updated_since=");
        url.push_str(&urlencode(since));
    }
    let body = get_json(events, state, &url).await?;
    let items = body
        .get("items")
        .cloned()
        .unwrap_or(serde_json::Value::Array(vec![]));
    serde_json::from_value::<Vec<DialogSync>>(items).map_err(|e| format!("bad items: {e}"))
}

/// `GET /api/v1/conversations/{id}/messages?limit=…`.
pub async fn fetch_messages(
    events: &dyn outbox::FlushEvents,
    state: &CacheState,
    dialog_id: &str,
    limit: u32,
) -> Result<Vec<MessageSync>, String> {
    let url = format!(
        "{}/api/v1/conversations/{}/messages?limit={}",
        state.api_base(),
        urlencode(dialog_id),
        limit.min(MSG_LIMIT)
    );
    let body = get_json(events, state, &url).await?;
    let items = body
        .get("items")
        .cloned()
        .unwrap_or(serde_json::Value::Array(vec![]));
    serde_json::from_value::<Vec<MessageSync>>(items).map_err(|e| format!("bad items: {e}"))
}

/// Минимальное percent-кодирование для значений query (uuid и ISO-даты).
fn urlencode(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 8);
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

// ---------------------------------------------------------------------------
// Проходы синхронизации
// ---------------------------------------------------------------------------

/// Курсор дельты: `meta.last_sync_at` минус перекрытие. `None` → нужна полная синхронизация.
fn delta_cursor(state: &CacheState) -> Option<String> {
    let raw = state.meta_get(META_LAST_SYNC).ok().flatten()?;
    let epoch = parse_iso(&raw)?;
    if now_epoch() - epoch > FULL_RESYNC_AFTER_SECS {
        return None; // спали дольше 30 минут — дешевле перезагрузить целиком
    }
    Some(format_iso(epoch - CURSOR_OVERLAP_SECS))
}

/// Новый курсор: максимальный серверный `updated_at` из выдачи (это серверное время,
/// поэтому расхождение часов на машине оператора не ломает дельту). Если диалогов
/// не пришло — оставляем прежний курсор, чтобы не проскочить изменения.
fn advance_cursor(state: &CacheState, dialogs: &[DialogSync]) -> Result<Option<String>, String> {
    let max = dialogs
        .iter()
        .filter_map(|d| d.updated_at.clone().or_else(|| d.last_message_at.clone()))
        .max();
    let next = match max {
        Some(v) => v,
        None => return state.meta_get(META_LAST_SYNC),
    };
    state.meta_set(META_LAST_SYNC, &next)?;
    Ok(Some(next))
}

/// Один проход: тянем список (полный или дельту), пишем в кэш, гоняем очередь.
async fn run(app: &AppHandle, state: &CacheState, force_full: bool) -> Result<SyncReport, String> {
    let report = run_with(app, state, force_full).await?;
    let _ = app.emit(EVENT_SYNCED, &report);
    Ok(report)
}

/// Тело прохода без Tauri-событий уровня окна (см. [`outbox::FlushEvents`]).
async fn run_with(
    events: &dyn outbox::FlushEvents,
    state: &CacheState,
    force_full: bool,
) -> Result<SyncReport, String> {
    let cursor = if force_full {
        None
    } else {
        delta_cursor(state)
    };
    let mut report = SyncReport {
        full: cursor.is_none(),
        ..Default::default()
    };

    match fetch_conversations(events, state, cursor.as_deref()).await {
        Ok(dialogs) => {
            report.dialogs = store::apply_sync(state, &dialogs)? as u32;
            report.last_sync_at = advance_cursor(state, &dialogs)?;
        }
        Err(e) if e == "unauthorized" || e == "no_session_token" => {
            // Токен протух — событие уже отправлено, фронт повторит синк после refresh.
            report.offline = false;
            return Ok(report);
        }
        Err(e) => {
            // Сети нет — это штатный офлайн, а не ошибка: UI живёт на кэше.
            eprintln!("[leadchat][sync] список диалогов недоступен: {e}");
            report.offline = true;
        }
    }

    report.flush = outbox::flush_with(events, state).await.ok();
    Ok(report)
}

/// Синхронизация при старте: полный список топ-200 + прогон очереди.
#[tauri::command]
pub async fn sync_startup(
    app: AppHandle,
    state: tauri::State<'_, CacheState>,
) -> Result<SyncReport, String> {
    run(&app, &state, true).await
}

/// Догон после reconnect WS: дельта по `updated_since` (или полный список,
/// если курсора нет или офлайн длился дольше 30 минут).
#[tauri::command]
pub async fn sync_reconnect(
    app: AppHandle,
    state: tauri::State<'_, CacheState>,
) -> Result<SyncReport, String> {
    run(&app, &state, false).await
}

/// Догрузить последние сообщения диалога в кэш (шаг 3 старта / открытие диалога).
#[tauri::command]
pub async fn sync_dialog_messages(
    app: AppHandle,
    state: tauri::State<'_, CacheState>,
    dialog_id: String,
    limit: Option<u32>,
) -> Result<u32, String> {
    let limit = limit.unwrap_or(MSG_LIMIT);
    let msgs = fetch_messages(&app, &state, &dialog_id, limit).await?;
    let n = store::upsert_messages(&state, &dialog_id, &msgs)?;
    Ok(n as u32)
}

/// Метка последней успешной синхронизации — для плашки «обновлено в …».
#[tauri::command]
pub async fn sync_last_at(state: tauri::State<'_, CacheState>) -> Result<Option<String>, String> {
    state.meta_get(META_LAST_SYNC)
}

// ---------------------------------------------------------------------------
// Фоновый таймер
// ---------------------------------------------------------------------------

/// Запустить фоновый прогон очереди раз в [`FLUSH_INTERVAL_SECS`] секунд.
///
/// Вызывается один раз из `setup(...)` в `lib.rs`. Отдельный поток (а не таймер
/// async-рантайма) — чтобы не тянуть зависимость на tokio-таймеры; сам flush
/// исполняется на рантайме Tauri через `block_on`.
pub fn start_flush_timer(app: AppHandle) {
    std::thread::Builder::new()
        .name("leadchat-outbox-flush".into())
        .spawn(move || loop {
            std::thread::sleep(std::time::Duration::from_secs(FLUSH_INTERVAL_SECS));
            let Some(state) = app.try_state::<CacheState>() else {
                continue;
            };
            match outbox::pending_count(&state) {
                Ok(0) => continue,
                Err(e) => {
                    eprintln!("[leadchat][sync] outbox недоступен: {e}");
                    continue;
                }
                Ok(_) => {}
            }
            let handle = app.clone();
            if let Err(e) =
                tauri::async_runtime::block_on(async move { outbox::flush_from_app(handle).await })
            {
                eprintln!("[leadchat][sync] flush по таймеру не удался: {e}");
            }
        })
        .map(|_| ())
        .unwrap_or_else(|e| eprintln!("[leadchat][sync] не удалось поднять таймер flush: {e}"));
}

/// Инициализация модуля синхронизации: сейчас — только фоновый таймер.
/// Первый полный синк инициирует фронт командой `sync_startup` (после логина).
pub fn init(app: &AppHandle) -> Result<(), String> {
    start_flush_timer(app.clone());
    Ok(())
}

/// Best-effort прогон очереди перед выходом (пункт трея «Выход», 04 §3.3).
pub fn flush_before_exit(app: &AppHandle, timeout_secs: u64) {
    let handle = app.clone();
    let done = std::thread::Builder::new()
        .name("leadchat-final-flush".into())
        .spawn(move || {
            let _ = tauri::async_runtime::block_on(outbox::flush_from_app(handle));
        });
    if let Ok(join) = done {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(timeout_secs);
        while !join.is_finished() && std::time::Instant::now() < deadline {
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
    }
}

/// Отметка «кэш живой» — используется в диагностике «О приложении».
pub fn cache_stamp(state: &CacheState) -> String {
    state
        .meta_get(META_LAST_SYNC)
        .ok()
        .flatten()
        .unwrap_or_else(now_iso)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cache::tests::{dead_port, mock_server, ready_state, test_state, TestEvents};
    use std::sync::atomic::Ordering;

    fn conversations_page(ids: &[(&str, &str)]) -> String {
        let items: Vec<serde_json::Value> = ids
            .iter()
            .map(|(id, updated)| {
                serde_json::json!({
                    "id": id, "status": "in_progress",
                    "account": {"id": "acc-1", "title": "LP-Москва"},
                    "client": {"id": "cl-1", "name": "Иван Петров", "phone": "+79261234567"},
                    "item": {"title": "Ремонт iPhone 13"},
                    "unread_count": 1,
                    "last_message_at": updated, "updated_at": updated
                })
            })
            .collect();
        serde_json::json!({"items": items, "page": {"limit": 200, "offset": 0, "total": items.len()}})
            .to_string()
    }

    fn run_sync(st: &CacheState, ev: &TestEvents, full: bool) -> SyncReport {
        tauri::async_runtime::block_on(run_with(ev, st, full)).unwrap()
    }

    #[test]
    fn startup_pulls_top_200_and_fills_cache() {
        let (base, seen) = mock_server(vec![(
            200,
            conversations_page(&[
                ("d1", "2026-08-05T10:00:00Z"),
                ("d2", "2026-08-05T12:00:00Z"),
            ]),
        )]);
        let st = ready_state(&base);
        let rep = run_sync(&st, &TestEvents::default(), true);

        assert!(rep.full);
        assert!(!rep.offline);
        assert_eq!(rep.dialogs, 2);
        assert_eq!(rep.last_sync_at.as_deref(), Some("2026-08-05T12:00:00Z"));

        let req = &seen.lock().unwrap()[0];
        assert!(
            req.starts_with("GET /api/v1/conversations?limit=200"),
            "{req}"
        );
        assert!(!req.contains("updated_since"), "старт — полный список");

        let cached = store::load_dialogs(&st).unwrap();
        assert_eq!(cached.len(), 2);
        assert_eq!(cached[0].id, "d2", "свежий диалог сверху");
        assert_eq!(cached[0].client_name.as_deref(), Some("Иван Петров"));
    }

    #[test]
    fn reconnect_sends_updated_since_cursor() {
        // ⚠ ВРЕМЯ ЗДЕСЬ СЧИТАЕТСЯ ОТ «СЕЙЧАС», А НЕ ЗАШИТО ДАТОЙ — И ЭТО НЕ
        // ПРИДИРКА. Раньше в тесте стояло 2026-08-05, и он был зелёным ровно
        // до 5 августа плюс полчаса: `delta_cursor` отдаёт None, когда курсор
        // старше FULL_RESYNC_AFTER_SECS, — то есть на любой более поздней
        // машине второй проход становился полным, и тест падал на «второй
        // проход — дельта». Проверял он при этом не поведение, а календарь.
        //
        // Хуже другого: тесты десктопа не гонялись ни в CI, ни при сборке
        // релиза (см. .github/workflows), поэтому падение никто не видел —
        // оно всплыло только при ручном прогоне 27.08.
        let свежий = now_epoch() - 60;
        let первый = format_iso(свежий);
        let второй = format_iso(свежий + 300);
        let ожидаемый = format_iso(свежий - CURSOR_OVERLAP_SECS).replace(':', "%3A");

        let (base, seen) = mock_server(vec![
            (200, conversations_page(&[("d1", &первый)])),
            (200, conversations_page(&[("d1", &второй)])),
        ]);
        let st = ready_state(&base);
        run_sync(&st, &TestEvents::default(), true);
        let rep = run_sync(&st, &TestEvents::default(), false);

        assert!(!rep.full, "второй проход — дельта");
        let reqs = seen.lock().unwrap();
        assert!(
            reqs[1].contains(&format!("updated_since={ожидаемый}")),
            "ждали курсор {ожидаемый}, запрос: {}",
            reqs[1]
        );
        assert_eq!(
            st.meta_get(META_LAST_SYNC).unwrap().as_deref(),
            Some(второй.as_str()),
            "курсор двигается по серверному updated_at"
        );
    }

    #[test]
    fn stale_cursor_falls_back_to_full_sync() {
        // Оборотная сторона того же правила, и раньше её не проверял никто:
        // курсор старше получаса обязан приводить к ПОЛНОМУ списку. Именно на
        // этой ветке молча держался протухший тест выше — значит поведение
        // нужно закрепить явно, а не полагаться на календарь.
        let древний = format_iso(now_epoch() - FULL_RESYNC_AFTER_SECS - 60);
        let свежий = format_iso(now_epoch());

        let (base, seen) = mock_server(vec![
            (200, conversations_page(&[("d1", &древний)])),
            (200, conversations_page(&[("d1", &свежий)])),
        ]);
        let st = ready_state(&base);
        run_sync(&st, &TestEvents::default(), true);
        let rep = run_sync(&st, &TestEvents::default(), false);

        assert!(rep.full, "курсор протух — тянем весь список");
        let reqs = seen.lock().unwrap();
        assert!(
            !reqs[1].contains("updated_since"),
            "полный список идёт без курсора, запрос: {}",
            reqs[1]
        );
    }

    #[test]
    fn offline_keeps_cache_and_reports_offline() {
        let (base, _) = mock_server(vec![(
            200,
            conversations_page(&[("d1", "2026-08-05T10:00:00Z")]),
        )]);
        let st = ready_state(&base);
        run_sync(&st, &TestEvents::default(), true);
        assert_eq!(store::load_dialogs(&st).unwrap().len(), 1);

        // «Выдернули сеть»
        st.set_api_base(&format!("http://127.0.0.1:{}", dead_port()));
        let rep = run_sync(&st, &TestEvents::default(), true);
        assert!(rep.offline, "офлайн — штатное состояние, не ошибка");
        assert_eq!(rep.dialogs, 0);
        assert_eq!(
            store::load_dialogs(&st).unwrap().len(),
            1,
            "кэш не затирается, когда сервер недоступен"
        );
    }

    #[test]
    fn unauthorized_asks_front_for_refresh() {
        let (base, _) = mock_server(vec![(401, "{}".to_string())]);
        let st = ready_state(&base);
        let ev = TestEvents::default();
        let rep = run_sync(&st, &ev, true);
        assert_eq!(rep.dialogs, 0);
        assert!(!rep.offline, "401 — это не офлайн");
        assert_eq!(ev.refresh.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn dialog_messages_are_fetched_and_cached() {
        let page = serde_json::json!({"items": [
            {"id": "m1", "conversation_id": "d1", "direction": "in", "sender_type": "client",
             "body": "Здравствуйте! Экран разбит, почём?", "attachments": [],
             "delivery_status": "delivered", "created_at": "2026-08-03T18:22:05Z"},
            {"id": "m2", "conversation_id": "d1", "direction": "out", "sender_type": "operator",
             "sender": {"id": "u1", "full_name": "Анна Смирнова"},
             "body": "Замена экрана — от 8 900 ₽", "attachments": [],
             "delivery_status": "delivered", "created_at": "2026-08-03T18:24:40Z"}
        ], "page": {}})
        .to_string();
        let (base, seen) = mock_server(vec![(200, page)]);
        let st = ready_state(&base);
        let ev = TestEvents::default();

        let msgs = tauri::async_runtime::block_on(fetch_messages(&ev, &st, "d1", 100)).unwrap();
        assert_eq!(msgs.len(), 2);
        store::upsert_messages(&st, "d1", &msgs).unwrap();

        assert!(
            seen.lock().unwrap()[0].starts_with("GET /api/v1/conversations/d1/messages?limit=100")
        );
        let view = store::load_messages(&st, "d1", 100).unwrap();
        assert_eq!(view.len(), 2);
        assert_eq!(
            view[0].body.as_deref(),
            Some("Здравствуйте! Экран разбит, почём?")
        );
        assert_eq!(view[1].sender_name.as_deref(), Some("Анна Смирнова"));
    }

    #[test]
    fn sync_also_drains_outbox() {
        let (base, seen) = mock_server(vec![
            (200, conversations_page(&[("d1", "2026-08-05T10:00:00Z")])),
            (
                201,
                "{\"id\":\"srv-1\",\"conversation_id\":\"d1\"}".to_string(),
            ),
        ]);
        let st = ready_state(&base);
        crate::cache::outbox::push(&st, "d1", "message", "накопилось в офлайне").unwrap();

        let rep = run_sync(&st, &TestEvents::default(), true);
        assert_eq!(rep.flush.as_ref().unwrap().sent, 1);
        assert!(crate::cache::outbox::list(&st).unwrap().is_empty());
        assert!(seen.lock().unwrap()[1].starts_with("POST /api/v1/conversations/d1/messages"));
    }

    #[test]
    fn no_token_short_circuits_without_network() {
        let st = test_state();
        st.set_api_base(&format!("http://127.0.0.1:{}", dead_port()));
        let ev = TestEvents::default();
        let rep = run_sync(&st, &ev, true);
        assert_eq!(rep.dialogs, 0);
        assert!(
            ev.refresh.load(Ordering::SeqCst) >= 1,
            "просим фронт дать токен"
        );
    }

    #[test]
    fn urlencode_escapes_query_values() {
        assert_eq!(
            urlencode("2026-08-05T10:00:00Z"),
            "2026-08-05T10%3A00%3A00Z"
        );
        assert_eq!(
            urlencode("c4e5f6a7-1234-4abc-8def-000000000000"),
            "c4e5f6a7-1234-4abc-8def-000000000000"
        );
        assert_eq!(urlencode("a b&c=d"), "a%20b%26c%3Dd");
    }

    #[test]
    fn delta_cursor_falls_back_to_full_sync() {
        let st = crate::cache::tests::test_state();
        assert!(delta_cursor(&st).is_none(), "без курсора — полный синк");

        st.meta_set(META_LAST_SYNC, &format_iso(now_epoch() - 60))
            .unwrap();
        let c = delta_cursor(&st).expect("свежий курсор годится для дельты");
        let epoch = parse_iso(&c).unwrap();
        assert!(
            (now_epoch() - epoch - 60 - CURSOR_OVERLAP_SECS).abs() <= 1,
            "курсор сдвинут назад на перекрытие"
        );

        st.meta_set(
            META_LAST_SYNC,
            &format_iso(now_epoch() - FULL_RESYNC_AFTER_SECS - 10),
        )
        .unwrap();
        assert!(delta_cursor(&st).is_none(), "долгий офлайн — полный синк");
    }

    #[test]
    fn cursor_advances_to_max_server_updated_at() {
        let st = crate::cache::tests::test_state();
        let mk = |id: &str, updated: &str| -> DialogSync {
            serde_json::from_value(serde_json::json!({
                "id": id, "status": "new", "updated_at": updated,
                "last_message_at": updated, "unread_count": 0
            }))
            .unwrap()
        };
        let dialogs = vec![
            mk("d1", "2026-08-05T10:00:00Z"),
            mk("d2", "2026-08-05T12:30:00Z"),
            mk("d3", "2026-08-05T11:00:00Z"),
        ];
        let next = advance_cursor(&st, &dialogs).unwrap();
        assert_eq!(next.as_deref(), Some("2026-08-05T12:30:00Z"));
        assert_eq!(
            st.meta_get(META_LAST_SYNC).unwrap().as_deref(),
            Some("2026-08-05T12:30:00Z")
        );

        // Пустая дельта не двигает курсор назад.
        let same = advance_cursor(&st, &[]).unwrap();
        assert_eq!(same.as_deref(), Some("2026-08-05T12:30:00Z"));
    }
}
