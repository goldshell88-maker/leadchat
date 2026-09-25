//! Исходящая очередь (04-DESKTOP §5.3) — единственные локально-авторитетные данные.
//!
//! Путь сообщения в десктопе ВСЕГДА один: `outbox_push` → строка в SQLite → `outbox_flush`.
//! Это убирает отдельную ветку «онлайн/офлайн» и делает отправку устойчивой к обрыву сети.
//!
//! Коды ответов (строго по §5.3):
//! * `2xx` — строка удаляется; вернувшееся сообщение кладётся в кэш;
//! * `401` — эмитим `session:refresh-needed`, проход прерываем, попытку НЕ засчитываем;
//! * `403 / 409 / 422` (и прочие 4xx, кроме ретраибельных) — `status='failed'` + `last_error`;
//! * `5xx`, сеть, таймаут, `408`, `429` — `attempts+1`,
//!   `next_try_at = now + min(2^attempts, 300)` сек; после 10 попыток — `failed`.
//!
//! Идемпотентность гарантирует бэкенд по `client_message_id` (01-API-SPEC §1.6):
//! повторная отправка той же строки не создаёт дубль у клиента.

use rusqlite::params;
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager};

use super::{format_iso, now_epoch, now_iso, store, CacheState};

/// После этого числа неудачных попыток строка уходит в `failed` (ручной повтор сбрасывает счётчик).
pub const MAX_ATTEMPTS: i64 = 10;
/// Потолок экспоненциального backoff, секунды.
pub const BACKOFF_CAP_SECS: i64 = 300;
/// Сколько строк берём за один проход flush.
pub const FLUSH_BATCH: i64 = 100;
/// Лимит длины текста (01 §6.2 — 413 при превышении).
pub const MAX_TEXT_LEN: usize = 4000;

/// Событие: access-JWT просрочен, фронту нужно сделать refresh и вызвать `set_session_token`.
pub const EVENT_SESSION_REFRESH: &str = "session:refresh-needed";
/// Событие: итоги прохода очереди (UI обновляет ⏳/✗).
pub const EVENT_OUTBOX_REPORT: &str = "outbox:report";

// ---------------------------------------------------------------------------
// Типы
// ---------------------------------------------------------------------------

/// Не отправленная строка очереди, показываемая в UI.
#[derive(Debug, Clone, Serialize)]
pub struct OutboxItemView {
    pub client_msg_id: String,
    pub dialog_id: String,
    pub kind: String,
    pub body: Option<String>,
    pub status: String,
    pub attempts: i64,
    pub next_try_at: Option<String>,
    pub last_error: Option<String>,
    pub created_at: String,
}

/// Строка, которую не примет ни один ретрай (403/409/422).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FailedItem {
    pub client_msg_id: String,
    pub dialog_id: String,
    pub error: String,
}

/// Отчёт о проходе очереди — уходит и в ответ команды, и событием `outbox:report`.
#[derive(Debug, Clone, Default, Serialize)]
pub struct FlushReport {
    /// Сколько строк ушло на сервер и удалено из очереди.
    pub sent: u32,
    /// Строки, отвергнутые сервером окончательно.
    pub failed: Vec<FailedItem>,
    /// Сколько осталось в очереди (нет сети / ждут backoff).
    pub remaining: u32,
    /// Проход не состоялся: другой flush уже идёт (таймер совпал с reconnect).
    pub skipped: bool,
    /// Нет access-JWT — фронт должен вызвать `set_session_token` после refresh.
    pub needs_token: bool,
}

/// Внутреннее представление строки, готовой к отправке (тело уже расшифровано).
struct DueItem {
    client_msg_id: String,
    dialog_id: String,
    kind: String,
    body: String,
    attempts: i64,
}

// ---------------------------------------------------------------------------
// Постановка в очередь
// ---------------------------------------------------------------------------

/// Поставить исходящее в очередь. Возвращает `client_msg_id` (uuid v4),
/// который фронт использует как `client_message_id` и как ключ ⏳-строки.
pub fn push(state: &CacheState, dialog_id: &str, kind: &str, body: &str) -> Result<String, String> {
    if dialog_id.trim().is_empty() {
        return Err("outbox: пустой dialog_id".to_string());
    }
    let kind = match kind {
        "note" => "note",
        "message" | "" => "message",
        other => return Err(format!("outbox: неизвестный kind '{other}'")),
    };
    let text = body.trim_end_matches(['\r', '\n']);
    if text.trim().is_empty() {
        return Err("outbox: пустой текст".to_string());
    }
    if text.chars().count() > MAX_TEXT_LEN {
        return Err(format!("outbox: текст длиннее {MAX_TEXT_LEN} символов"));
    }

    let client_msg_id = uuid::Uuid::new_v4().to_string();
    let body_enc = state.crypto().encrypt_str(text)?;
    let conn = state.conn()?;
    conn.execute(
        "INSERT INTO outbox (client_msg_id, dialog_id, kind, body_enc, status,
                             attempts, next_try_at, created_at)
         VALUES (?1, ?2, ?3, ?4, 'pending', 0, NULL, ?5)",
        params![client_msg_id, dialog_id, kind, body_enc, now_iso()],
    )
    .map_err(|e| e.to_string())?;
    Ok(client_msg_id)
}

/// Вариант [`push`] для мест, где под рукой только `AppHandle`
/// (быстрый ответ из WinRT-тоста, `notify.rs`).
pub fn push_from_app(
    app: &AppHandle,
    dialog_id: &str,
    kind: &str,
    body: &str,
) -> Result<String, String> {
    let state = app
        .try_state::<CacheState>()
        .ok_or_else(|| "cache не инициализирован".to_string())?;
    push(&state, dialog_id, kind, body)
}

// ---------------------------------------------------------------------------
// Чтение / ручное управление очередью
// ---------------------------------------------------------------------------

/// Вся очередь для UI (тексты расшифрованы — это собственные сообщения оператора).
pub fn list(state: &CacheState) -> Result<Vec<OutboxItemView>, String> {
    let conn = state.conn()?;
    let mut stmt = conn
        .prepare(
            "SELECT client_msg_id, dialog_id, kind, body_enc, status,
                    attempts, next_try_at, last_error, created_at
             FROM outbox ORDER BY id ASC",
        )
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([], |row| {
            Ok(OutboxItemView {
                client_msg_id: row.get(0)?,
                dialog_id: row.get(1)?,
                kind: row.get(2)?,
                body: state.crypto().decrypt_opt(row.get(3)?),
                status: row.get(4)?,
                attempts: row.get(5)?,
                next_try_at: row.get(6)?,
                last_error: row.get(7)?,
                created_at: row.get(8)?,
            })
        })
        .map_err(|e| e.to_string())?;
    let mut out = Vec::new();
    for r in rows {
        out.push(r.map_err(|e| e.to_string())?);
    }
    Ok(out)
}

/// Кнопка «Повторить»: сбрасывает счётчик попыток и возвращает строку в очередь.
pub fn retry(state: &CacheState, client_msg_id: &str) -> Result<bool, String> {
    let conn = state.conn()?;
    let n = conn
        .execute(
            "UPDATE outbox
             SET status='pending', attempts=0, next_try_at=NULL, last_error=NULL
             WHERE client_msg_id = ?1",
            [client_msg_id],
        )
        .map_err(|e| e.to_string())?;
    Ok(n > 0)
}

/// Кнопка «Удалить»: убрать строку из очереди навсегда.
pub fn delete(state: &CacheState, client_msg_id: &str) -> Result<bool, String> {
    let conn = state.conn()?;
    let n = conn
        .execute(
            "DELETE FROM outbox WHERE client_msg_id = ?1",
            [client_msg_id],
        )
        .map_err(|e| e.to_string())?;
    Ok(n > 0)
}

/// Сколько строк ждёт отправки (для решения «запускать ли flush по таймеру»).
pub fn pending_count(state: &CacheState) -> Result<u32, String> {
    let conn = state.conn()?;
    conn.query_row(
        "SELECT COUNT(*) FROM outbox WHERE status = 'pending'",
        [],
        |r| r.get::<_, i64>(0),
    )
    .map(|n| n as u32)
    .map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------
// Flush
// ---------------------------------------------------------------------------

/// Строки, которым пришло время: `pending` и `next_try_at <= now`.
fn due_items(state: &CacheState) -> Result<Vec<DueItem>, String> {
    let now = now_iso();
    let conn = state.conn()?;
    let mut stmt = conn
        .prepare(
            "SELECT client_msg_id, dialog_id, kind, body_enc, attempts
             FROM outbox
             WHERE status = 'pending' AND (next_try_at IS NULL OR next_try_at <= ?1)
             ORDER BY created_at ASC, id ASC
             LIMIT ?2",
        )
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map(params![now, FLUSH_BATCH], |row| {
            let blob: Vec<u8> = row.get(3)?;
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                blob,
                row.get::<_, i64>(4)?,
            ))
        })
        .map_err(|e| e.to_string())?;

    let mut out = Vec::new();
    for r in rows {
        let (client_msg_id, dialog_id, kind, blob, attempts) = r.map_err(|e| e.to_string())?;
        // Нерасшифровываемую строку (сменился профиль Windows) тихо пропускаем —
        // её подберёт следующий проход только после ручного вмешательства.
        match state.crypto().decrypt_str(&blob) {
            Ok(body) => out.push(DueItem {
                client_msg_id,
                dialog_id,
                kind,
                body,
                attempts,
            }),
            Err(e) => {
                mark_failed(state, &client_msg_id, &format!("decrypt: {e}"))?;
            }
        }
    }
    Ok(out)
}

fn set_status(state: &CacheState, client_msg_id: &str, status: &str) -> Result<(), String> {
    let conn = state.conn()?;
    conn.execute(
        "UPDATE outbox SET status = ?2 WHERE client_msg_id = ?1",
        params![client_msg_id, status],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

fn mark_failed(state: &CacheState, client_msg_id: &str, err: &str) -> Result<(), String> {
    let conn = state.conn()?;
    conn.execute(
        "UPDATE outbox SET status='failed', last_error=?2 WHERE client_msg_id = ?1",
        params![client_msg_id, err],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

/// `attempts+1`; `next_try_at = now + min(2^attempts, 300)`; после [`MAX_ATTEMPTS`] — `failed`.
fn retry_later(
    state: &CacheState,
    client_msg_id: &str,
    attempts: i64,
    err: &str,
) -> Result<bool, String> {
    let attempts = attempts + 1;
    if attempts >= MAX_ATTEMPTS {
        mark_failed(
            state,
            client_msg_id,
            &format!("{err} (исчерпаны {MAX_ATTEMPTS} попыток)"),
        )?;
        let conn = state.conn()?;
        conn.execute(
            "UPDATE outbox SET attempts=?2 WHERE client_msg_id=?1",
            params![client_msg_id, attempts],
        )
        .map_err(|e| e.to_string())?;
        return Ok(false);
    }
    let delay = (1i64 << attempts.min(20)).min(BACKOFF_CAP_SECS);
    let next = format_iso(now_epoch() + delay);
    let conn = state.conn()?;
    conn.execute(
        "UPDATE outbox
         SET status='pending', attempts=?2, next_try_at=?3, last_error=?4
         WHERE client_msg_id=?1",
        params![client_msg_id, attempts, next, err],
    )
    .map_err(|e| e.to_string())?;
    Ok(true)
}

/// Ретраибельные HTTP-коды: временные по смыслу, а не «запрос неверный».
fn is_retryable(status: u16) -> bool {
    status >= 500 || status == 408 || status == 429
}

/// Куда flush отправляет события. В приложении — `AppHandle`; отдельный трейт нужен,
/// чтобы проход очереди можно было прогнать в тестах без запуска WebView.
pub trait FlushEvents: Send + Sync {
    /// Access-JWT протух или отсутствует.
    fn session_refresh_needed(&self);
    /// Итоги прохода.
    fn report(&self, report: &FlushReport);
}

impl FlushEvents for AppHandle {
    fn session_refresh_needed(&self) {
        let _ = self.emit(EVENT_SESSION_REFRESH, ());
    }
    fn report(&self, report: &FlushReport) {
        let _ = self.emit(EVENT_OUTBOX_REPORT, report);
    }
}

/// Прогнать очередь: по одному POST на строку, в порядке постановки.
pub async fn flush(app: &AppHandle, state: &CacheState) -> Result<FlushReport, String> {
    flush_with(app, state).await
}

/// Реализация прохода очереди, независимая от Tauri-событий (см. [`FlushEvents`]).
pub async fn flush_with(
    events: &dyn FlushEvents,
    state: &CacheState,
) -> Result<FlushReport, String> {
    let mut report = FlushReport::default();

    let Some(_guard) = state.try_flush_guard() else {
        report.skipped = true;
        report.remaining = pending_count(state)?;
        return Ok(report);
    };

    let items = due_items(state)?;
    if items.is_empty() {
        report.remaining = pending_count(state)?;
        return Ok(report);
    }

    let token = match state.session_token() {
        Ok(t) => t,
        Err(_) => {
            // Токена нет (ещё не залогинились / после рестарта) — не тратим попытки.
            events.session_refresh_needed();
            report.needs_token = true;
            report.remaining = pending_count(state)?;
            events.report(&report);
            return Ok(report);
        }
    };

    let base = state.api_base();

    for item in items {
        let path = if item.kind == "note" {
            "notes"
        } else {
            "messages"
        };
        let url = format!("{base}/api/v1/conversations/{}/{}", item.dialog_id, path);
        let payload = serde_json::json!({
            "text": item.body,
            "client_message_id": item.client_msg_id,
        });

        set_status(state, &item.client_msg_id, "sending")?;

        let resp = state
            .http()
            .post(&url)
            .bearer_auth(&token)
            .json(&payload)
            .timeout(std::time::Duration::from_secs(super::HTTP_TIMEOUT_SECS))
            .send()
            .await;

        match resp {
            Ok(r) if r.status().is_success() => {
                // Сервер вернул полноценный message — сразу кладём его в кэш,
                // чтобы ⏳ сменилось на ✓ даже без WS-события.
                let created = r.json::<serde_json::Value>().await.ok();
                delete(state, &item.client_msg_id)?;
                if let Some(v) = created {
                    if let Ok(msg) = serde_json::from_value::<store::MessageSync>(v) {
                        let _ = store::upsert_messages(state, &item.dialog_id, &[msg]);
                    }
                }
                report.sent += 1;
            }
            Ok(r) if r.status().as_u16() == 401 => {
                // Токен протух: возвращаем строку в очередь, попытку не засчитываем,
                // просим фронт обновить токен и прерываем проход.
                set_status(state, &item.client_msg_id, "pending")?;
                events.session_refresh_needed();
                report.needs_token = true;
                break;
            }
            Ok(r) if is_retryable(r.status().as_u16()) => {
                let err = format!("HTTP {}", r.status().as_u16());
                let requeued = retry_later(state, &item.client_msg_id, item.attempts, &err)?;
                if !requeued {
                    report.failed.push(FailedItem {
                        client_msg_id: item.client_msg_id.clone(),
                        dialog_id: item.dialog_id.clone(),
                        error: err,
                    });
                }
                // Сервер/сеть не в порядке — остальные строки не дёргаем.
                break;
            }
            Ok(r) => {
                // 403 / 409 / 422 и прочие клиентские — ретраить бессмысленно.
                let status = r.status().as_u16();
                let code = r
                    .json::<serde_json::Value>()
                    .await
                    .ok()
                    .and_then(|v| {
                        v.get("error")
                            .and_then(|e| e.get("code"))
                            .and_then(|c| c.as_str().map(str::to_string))
                    })
                    .unwrap_or_default();
                let err = if code.is_empty() {
                    format!("HTTP {status}")
                } else {
                    format!("HTTP {status}: {code}")
                };
                mark_failed(state, &item.client_msg_id, &err)?;
                report.failed.push(FailedItem {
                    client_msg_id: item.client_msg_id.clone(),
                    dialog_id: item.dialog_id.clone(),
                    error: err,
                });
            }
            Err(e) => {
                // Сеть/таймаут/DNS — backoff и выходим: сети нет, остальные не трогаем.
                let err = if e.is_timeout() {
                    "timeout".to_string()
                } else {
                    format!("network: {e}")
                };
                let requeued = retry_later(state, &item.client_msg_id, item.attempts, &err)?;
                if !requeued {
                    report.failed.push(FailedItem {
                        client_msg_id: item.client_msg_id.clone(),
                        dialog_id: item.dialog_id.clone(),
                        error: err,
                    });
                }
                break;
            }
        }
    }

    // Всё, что осталось в 'sending' после прерванного прохода, возвращаем в очередь.
    {
        let conn = state.conn()?;
        conn.execute(
            "UPDATE outbox SET status='pending' WHERE status='sending'",
            [],
        )
        .map_err(|e| e.to_string())?;
    }

    report.remaining = pending_count(state)?;
    events.report(&report);
    Ok(report)
}

/// Вариант [`flush`] для мест, где есть только `AppHandle`
/// (таймер синка, быстрый ответ из тоста, best-effort перед выходом).
pub async fn flush_from_app(app: AppHandle) -> Result<FlushReport, String> {
    let state = app
        .try_state::<CacheState>()
        .ok_or_else(|| "cache не инициализирован".to_string())?;
    flush(&app, &state).await
}

// ---------------------------------------------------------------------------
// Команды (04 §8.1)
// ---------------------------------------------------------------------------

/// Поставить исходящее в очередь; возвращает `client_msg_id`.
#[tauri::command]
pub async fn outbox_push(
    state: tauri::State<'_, CacheState>,
    dialog_id: String,
    kind: String,
    body: String,
) -> Result<String, String> {
    push(&state, &dialog_id, &kind, &body)
}

/// Прогнать очередь: POST с `client_message_id` в теле (01 §1.6), backoff, статусы.
#[tauri::command]
pub async fn outbox_flush(
    app: AppHandle,
    state: tauri::State<'_, CacheState>,
) -> Result<FlushReport, String> {
    flush(&app, &state).await
}

/// Очередь для UI (⏳ и ✗ строки).
#[tauri::command]
pub async fn outbox_list(
    state: tauri::State<'_, CacheState>,
) -> Result<Vec<OutboxItemView>, String> {
    list(&state)
}

/// «Повторить» для failed-строки.
#[tauri::command]
pub async fn outbox_retry(
    state: tauri::State<'_, CacheState>,
    client_msg_id: String,
) -> Result<bool, String> {
    retry(&state, &client_msg_id)
}

/// «Удалить» строку из очереди.
#[tauri::command]
pub async fn outbox_delete(
    state: tauri::State<'_, CacheState>,
    client_msg_id: String,
) -> Result<bool, String> {
    delete(&state, &client_msg_id)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cache::tests::test_state;

    #[test]
    fn push_returns_uuid_and_encrypts_body() {
        let st = test_state();
        let id = push(&st, "d1", "message", "Здравствуйте!").unwrap();
        assert_eq!(id.len(), 36, "uuid v4 в каноническом виде");
        assert!(uuid::Uuid::parse_str(&id).is_ok());

        let blob: Vec<u8> = st
            .conn()
            .unwrap()
            .query_row(
                "SELECT body_enc FROM outbox WHERE client_msg_id=?1",
                [&id],
                |r| r.get(0),
            )
            .unwrap();
        assert!(!String::from_utf8_lossy(&blob).contains("Здравствуйте"));

        let items = list(&st).unwrap();
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].body.as_deref(), Some("Здравствуйте!"));
        assert_eq!(items[0].status, "pending");
        assert_eq!(items[0].attempts, 0);
    }

    #[test]
    fn push_validates_input() {
        let st = test_state();
        assert!(push(&st, "d1", "message", "   ").is_err());
        assert!(push(&st, "", "message", "текст").is_err());
        assert!(push(&st, "d1", "wat", "текст").is_err());
        assert!(push(&st, "d1", "note", &"я".repeat(MAX_TEXT_LEN + 1)).is_err());
        assert!(push(&st, "d1", "note", &"я".repeat(MAX_TEXT_LEN)).is_ok());
    }

    #[test]
    fn client_msg_id_is_unique_per_push() {
        let st = test_state();
        let a = push(&st, "d1", "message", "раз").unwrap();
        let b = push(&st, "d1", "message", "раз").unwrap();
        assert_ne!(a, b, "uuid на каждое нажатие «отправить» (01 §1.6)");
    }

    #[test]
    fn due_items_respect_backoff() {
        let st = test_state();
        let id = push(&st, "d1", "message", "текст").unwrap();
        assert_eq!(due_items(&st).unwrap().len(), 1);

        retry_later(&st, &id, 0, "HTTP 500").unwrap();
        assert!(due_items(&st).unwrap().is_empty(), "ждём next_try_at");

        let items = list(&st).unwrap();
        assert_eq!(items[0].attempts, 1);
        assert_eq!(items[0].last_error.as_deref(), Some("HTTP 500"));
        assert!(items[0].next_try_at.is_some());
    }

    #[test]
    fn backoff_is_exponential_and_capped() {
        let st = test_state();
        let id = push(&st, "d1", "message", "текст").unwrap();
        let delay_for = |attempts: i64| -> i64 {
            retry_later(&st, &id, attempts, "HTTP 503").unwrap();
            let next = list(&st).unwrap()[0].next_try_at.clone().unwrap();
            crate::cache::parse_iso(&next).unwrap() - now_epoch()
        };
        assert!((delay_for(0) - 2).abs() <= 1, "2^1 = 2 c");
        assert!((delay_for(3) - 16).abs() <= 1, "2^4 = 16 c");
        assert!(
            (delay_for(8) - BACKOFF_CAP_SECS).abs() <= 1,
            "потолок 300 c"
        );
    }

    #[test]
    fn tenth_attempt_marks_failed() {
        let st = test_state();
        let id = push(&st, "d1", "message", "текст").unwrap();
        let requeued = retry_later(&st, &id, MAX_ATTEMPTS - 1, "HTTP 500").unwrap();
        assert!(!requeued);
        let items = list(&st).unwrap();
        assert_eq!(items[0].status, "failed");
        assert_eq!(items[0].attempts, MAX_ATTEMPTS);
        assert!(items[0].last_error.as_ref().unwrap().contains("попыток"));
        assert!(
            due_items(&st).unwrap().is_empty(),
            "failed не ретраится сам"
        );
    }

    #[test]
    fn retry_resets_counter_and_delete_removes() {
        let st = test_state();
        let id = push(&st, "d1", "message", "текст").unwrap();
        mark_failed(&st, &id, "HTTP 422").unwrap();
        assert!(retry(&st, &id).unwrap());
        let items = list(&st).unwrap();
        assert_eq!(items[0].status, "pending");
        assert_eq!(items[0].attempts, 0);
        assert!(items[0].last_error.is_none());

        assert!(delete(&st, &id).unwrap());
        assert!(list(&st).unwrap().is_empty());
        assert!(!delete(&st, &id).unwrap(), "повторное удаление — no-op");
    }

    #[test]
    fn retryable_codes_are_classified_per_spec() {
        for code in [500u16, 502, 503, 504, 408, 429] {
            assert!(is_retryable(code), "{code} должен ретраиться");
        }
        for code in [400u16, 403, 404, 409, 413, 422] {
            assert!(!is_retryable(code), "{code} ретраить нельзя");
        }
    }

    #[test]
    fn incoming_message_dedups_pending_row() {
        let st = test_state();
        let id = push(&st, "d1", "message", "уже доставлено").unwrap();
        // Сервер принял, ответ 2xx потерялся; сообщение приезжает синком/WS.
        let msg: store::MessageSync = serde_json::from_value(serde_json::json!({
            "id": "srv-1",
            "conversation_id": "d1",
            "direction": "out",
            "sender_type": "operator",
            "body": "уже доставлено",
            "delivery_status": "delivered",
            "client_message_id": id,
            "created_at": "2026-08-05T10:00:00Z"
        }))
        .unwrap();
        store::upsert_messages(&st, "d1", &[msg]).unwrap();
        assert!(
            list(&st).unwrap().is_empty(),
            "⏳-строка снята по client_message_id"
        );
    }

    #[test]
    fn pending_rows_appear_in_thread() {
        let st = test_state();
        push(&st, "d1", "message", "черновик").unwrap();
        let view = store::load_messages(&st, "d1", 100).unwrap();
        assert_eq!(view.len(), 1);
        assert!(view[0].pending);
        assert_eq!(view[0].direction, "out");
        assert_eq!(view[0].delivery_status, "pending");
        assert_eq!(view[0].body.as_deref(), Some("черновик"));
    }

    #[test]
    fn note_kind_renders_as_note() {
        let st = test_state();
        push(&st, "d1", "note", "торгуется").unwrap();
        let view = store::load_messages(&st, "d1", 100).unwrap();
        assert_eq!(view[0].direction, "note");
    }

    // -----------------------------------------------------------------------
    // Сквозные проверки прохода очереди против мок-сервера
    // -----------------------------------------------------------------------

    use crate::cache::tests::{dead_port, mock_server, ready_state, TestEvents};
    use std::sync::atomic::Ordering;

    fn run_flush(st: &CacheState, ev: &TestEvents) -> FlushReport {
        tauri::async_runtime::block_on(flush_with(ev, st)).unwrap()
    }

    #[test]
    fn success_removes_row_and_caches_server_message() {
        let created = serde_json::json!({
            "id": "srv-1", "conversation_id": "d1", "direction": "out",
            "sender_type": "operator", "sender": {"id": "u1", "full_name": "Анна Смирнова"},
            "body": "Добрый день!", "delivery_status": "pending",
            "created_at": "2026-08-05T10:12:00Z"
        });
        let (base, seen) = mock_server(vec![(201, created.to_string())]);
        let st = ready_state(&base);
        let cmid = push(&st, "d1", "message", "Добрый день!").unwrap();

        let ev = TestEvents::default();
        let rep = run_flush(&st, &ev);

        assert_eq!(rep.sent, 1);
        assert_eq!(rep.remaining, 0);
        assert!(rep.failed.is_empty());
        assert!(list(&st).unwrap().is_empty(), "строка удалена из очереди");

        let req = &seen.lock().unwrap()[0];
        assert!(
            req.starts_with("POST /api/v1/conversations/d1/messages"),
            "{req}"
        );
        assert!(
            req.contains("authorization: Bearer jwt-access")
                || req.contains("Authorization: Bearer jwt-access")
        );
        assert!(
            req.contains(&format!("\"client_message_id\":\"{cmid}\"")),
            "идемпотентность 01 §1.6: {req}"
        );

        // Ответ сервера сразу лёг в кэш — ⏳ сменилось на подтверждённое сообщение.
        let view = store::load_messages(&st, "d1", 100).unwrap();
        assert_eq!(view.len(), 1);
        assert!(!view[0].pending);
        assert_eq!(view[0].id, "srv-1");
        assert_eq!(view[0].sender_name.as_deref(), Some("Анна Смирнова"));
    }

    #[test]
    fn note_goes_to_notes_endpoint() {
        let (base, seen) = mock_server(vec![(201, "{\"id\":\"n1\"}".to_string())]);
        let st = ready_state(&base);
        push(&st, "d7", "note", "торгуется").unwrap();
        run_flush(&st, &TestEvents::default());
        assert!(seen.lock().unwrap()[0].starts_with("POST /api/v1/conversations/d7/notes"));
    }

    #[test]
    fn unauthorized_requeues_without_spending_attempt() {
        let (base, seen) = mock_server(vec![(401, "{}".to_string())]);
        let st = ready_state(&base);
        push(&st, "d1", "message", "текст").unwrap();
        push(&st, "d1", "message", "второй").unwrap();

        let ev = TestEvents::default();
        let rep = run_flush(&st, &ev);

        assert!(rep.needs_token, "фронт должен обновить токен");
        assert_eq!(rep.sent, 0);
        assert_eq!(rep.remaining, 2);
        assert_eq!(ev.refresh.load(Ordering::SeqCst), 1);
        assert_eq!(
            seen.lock().unwrap().len(),
            1,
            "проход прерван на первой строке"
        );

        let items = list(&st).unwrap();
        assert!(
            items.iter().all(|i| i.status == "pending"),
            "строки вернулись в очередь"
        );
        assert!(
            items.iter().all(|i| i.attempts == 0),
            "401 не считается попыткой"
        );
    }

    #[test]
    fn missing_token_asks_for_refresh_and_keeps_queue() {
        let st = test_state();
        push(&st, "d1", "message", "текст").unwrap();
        let ev = TestEvents::default();
        let rep = run_flush(&st, &ev);
        assert!(rep.needs_token);
        assert_eq!(rep.remaining, 1);
        assert_eq!(ev.refresh.load(Ordering::SeqCst), 1);
        assert_eq!(list(&st).unwrap()[0].attempts, 0);
    }

    #[test]
    fn client_error_fails_permanently() {
        let body = serde_json::json!({
            "error": {"code": "unprocessable", "message": "диалог закрыт"}
        });
        let (base, _) = mock_server(vec![(422, body.to_string())]);
        let st = ready_state(&base);
        let cmid = push(&st, "d1", "message", "текст").unwrap();

        let rep = run_flush(&st, &TestEvents::default());
        assert_eq!(rep.sent, 0);
        assert_eq!(rep.failed.len(), 1);
        assert_eq!(rep.failed[0].client_msg_id, cmid);
        assert!(rep.failed[0].error.contains("422"));
        assert!(rep.failed[0].error.contains("unprocessable"));

        let items = list(&st).unwrap();
        assert_eq!(items[0].status, "failed");
        assert!(
            due_items(&st).unwrap().is_empty(),
            "failed сам не ретраится"
        );
    }

    #[test]
    fn server_error_backs_off_and_stops_the_pass() {
        let (base, seen) = mock_server(vec![(503, "{}".to_string())]);
        let st = ready_state(&base);
        push(&st, "d1", "message", "первое").unwrap();
        push(&st, "d1", "message", "второе").unwrap();

        let rep = run_flush(&st, &TestEvents::default());
        assert_eq!(rep.sent, 0);
        assert_eq!(rep.remaining, 2);
        assert!(rep.failed.is_empty(), "5xx — не окончательный отказ");
        assert_eq!(
            seen.lock().unwrap().len(),
            1,
            "сети нет — остальные не трогаем"
        );

        let items = list(&st).unwrap();
        assert_eq!(items[0].attempts, 1);
        assert_eq!(items[0].last_error.as_deref(), Some("HTTP 503"));
        assert!(items[0].next_try_at.is_some());
        assert_eq!(items[1].attempts, 0, "вторая строка не тронута");
    }

    #[test]
    fn network_failure_backs_off() {
        // Порт, на котором никто не слушает: connection refused.
        let st = ready_state(&format!("http://127.0.0.1:{}", dead_port()));
        push(&st, "d1", "message", "офлайн-черновик").unwrap();

        let rep = run_flush(&st, &TestEvents::default());
        assert_eq!(rep.sent, 0);
        assert_eq!(rep.remaining, 1);
        let items = list(&st).unwrap();
        assert_eq!(items[0].status, "pending", "строка не потеряна");
        assert_eq!(items[0].attempts, 1);
        assert!(items[0].last_error.as_ref().unwrap().contains("network"));
    }

    #[test]
    fn retry_reuses_same_client_message_id() {
        // 503 → backoff; ручной «Повторить» → 201. Сервер обязан увидеть один и тот же
        // client_message_id (иначе идемпотентность 01 §1.6 не спасёт от дубля).
        let (base, seen) = mock_server(vec![
            (503, "{}".to_string()),
            (
                201,
                "{\"id\":\"srv-1\",\"conversation_id\":\"d1\"}".to_string(),
            ),
        ]);
        let st = ready_state(&base);
        let cmid = push(&st, "d1", "message", "текст").unwrap();

        run_flush(&st, &TestEvents::default());
        retry(&st, &cmid).unwrap(); // кнопка «Повторить» сбрасывает backoff
        let rep = run_flush(&st, &TestEvents::default());

        assert_eq!(rep.sent, 1);
        let reqs = seen.lock().unwrap();
        assert_eq!(reqs.len(), 2);
        for r in reqs.iter() {
            assert!(r.contains(&format!("\"client_message_id\":\"{cmid}\"")));
        }
        assert!(list(&st).unwrap().is_empty());
    }

    #[test]
    fn parallel_flush_is_skipped_not_duplicated() {
        let st = test_state();
        let _guard = st.try_flush_guard().expect("первый проход занял лок");
        push(&st, "d1", "message", "текст").unwrap();
        let rep = run_flush(&st, &TestEvents::default());
        assert!(rep.skipped, "второй одновременный flush не шлёт ничего");
        assert_eq!(rep.sent, 0);
        assert_eq!(rep.remaining, 1);
    }
}
