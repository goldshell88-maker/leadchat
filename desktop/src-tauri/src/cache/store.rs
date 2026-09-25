//! Проекция сервера в SQLite: `dialogs` и `cached_messages` (04-DESKTOP §5.1–§5.4).
//!
//! Правила, из которых всё следует:
//! * кэш — только для чтения; конфликтов не бывает, потому что локальных правок
//!   серверных сущностей не существует. Любой upsert — «сервер прав», перезапись;
//! * лимиты жёсткие: [`CACHE_DIALOGS`] диалогов и [`CACHE_MSGS_PER_DIALOG`] сообщений
//!   на диалог, prune после каждой синхронизации;
//! * содержательные поля (имя, телефон, название объявления, тексты, карточка)
//!   лежат в колонках `*_enc` — шифруются на входе, расшифровываются на выходе.

use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};

use super::{now_iso, CacheState};

/// Максимум диалогов в кэше (04 §5.1).
pub const CACHE_DIALOGS: usize = 200;
/// Максимум сообщений на диалог (04 §5.1).
pub const CACHE_MSGS_PER_DIALOG: usize = 100;

// ---------------------------------------------------------------------------
// Типы обмена с фронтом
// ---------------------------------------------------------------------------

/// Ссылка вида `{id, title}` / `{id, full_name}` из ответов API.
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct RefObj {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub full_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub phone: Option<String>,
    #[serde(flatten, default, skip_serializing_if = "serde_json::Map::is_empty")]
    pub extra: serde_json::Map<String, serde_json::Value>,
}

/// Элемент `GET /conversations` (01-API-SPEC §5.1) «как есть».
///
/// Фронт передаёт items ответа без переупаковки; неизвестные поля сохраняются
/// в `extra` и попадают в зашифрованную карточку — при добавлении полей на сервере
/// кэш не нужно менять.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct DialogSync {
    pub id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub account: Option<RefObj>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client: Option<RefObj>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub assignee: Option<RefObj>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub item: Option<RefObj>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unread_count: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_message_at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub updated_at: Option<String>,
    #[serde(flatten, default, skip_serializing_if = "serde_json::Map::is_empty")]
    pub extra: serde_json::Map<String, serde_json::Value>,
}

/// Строка диалога для UI — уже расшифрованная.
#[derive(Debug, Clone, Serialize)]
pub struct DialogView {
    pub id: String,
    pub account_id: Option<String>,
    pub account_title: Option<String>,
    pub status: String,
    pub assignee_id: Option<String>,
    pub assignee_name: Option<String>,
    pub unread: i64,
    pub last_message_at: Option<String>,
    pub updated_at: Option<String>,
    pub client_name: Option<String>,
    pub client_phone: Option<String>,
    pub item_title: Option<String>,
    /// Полный JSON элемента списка — той же формы, что отдаёт `GET /conversations`,
    /// чтобы UI рисовал офлайн-снапшот своим обычным рендерером.
    pub card: Option<serde_json::Value>,
}

/// Сообщение из `GET /conversations/{id}/messages` (01 §6.1) «как есть».
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct MessageSync {
    pub id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub conversation_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub direction: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sender_type: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sender: Option<RefObj>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub body: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub attachments: Option<serde_json::Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delivery_status: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client_message_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub created_at: Option<String>,
}

/// Сообщение для UI — расшифрованное. `pending = true` у ⏳-строк из outbox.
#[derive(Debug, Clone, Serialize)]
pub struct MessageView {
    pub id: String,
    pub dialog_id: String,
    pub direction: String,
    pub sender_type: String,
    pub sender_name: Option<String>,
    pub body: Option<String>,
    pub attachments: Option<serde_json::Value>,
    pub delivery_status: String,
    pub client_message_id: Option<String>,
    pub created_at: String,
    /// Строка ещё не подтверждена сервером (лежит в outbox).
    pub pending: bool,
}

// ---------------------------------------------------------------------------
// Чтение
// ---------------------------------------------------------------------------

/// Мгновенная отрисовка списка из SQLite (шаг 1 старта, 04 §5.4).
pub fn load_dialogs(state: &CacheState) -> Result<Vec<DialogView>, String> {
    let conn = state.conn()?;
    let mut stmt = conn
        .prepare(
            "SELECT id, account_id, account_title, status, assignee_id, assignee_name,
                    unread, last_message_at, updated_at,
                    client_name_enc, client_phone_enc, item_title_enc, card_json_enc
             FROM dialogs
             ORDER BY COALESCE(last_message_at, updated_at, '') DESC
             LIMIT ?1",
        )
        .map_err(|e| e.to_string())?;

    let rows = stmt
        .query_map([CACHE_DIALOGS as i64], |row| {
            Ok(DialogView {
                id: row.get(0)?,
                account_id: row.get(1)?,
                account_title: row.get(2)?,
                status: row.get(3)?,
                assignee_id: row.get(4)?,
                assignee_name: row.get(5)?,
                unread: row.get(6)?,
                last_message_at: row.get(7)?,
                updated_at: row.get(8)?,
                client_name: state.crypto().decrypt_opt(row.get(9)?),
                client_phone: state.crypto().decrypt_opt(row.get(10)?),
                item_title: state.crypto().decrypt_opt(row.get(11)?),
                card: state.crypto().decrypt_json(row.get(12)?),
            })
        })
        .map_err(|e| e.to_string())?;

    let mut out = Vec::new();
    for r in rows {
        out.push(r.map_err(|e| e.to_string())?);
    }
    Ok(out)
}

/// Сумма непрочитанных по кэшу — источник бейджа в офлайне (04 §3.2).
pub fn unread_total(state: &CacheState) -> Result<i64, String> {
    let conn = state.conn()?;
    conn.query_row("SELECT COALESCE(SUM(unread), 0) FROM dialogs", [], |r| {
        r.get(0)
    })
    .map_err(|e| e.to_string())
}

/// Лента диалога из кэша + ⏳-строки из outbox (хвостом, в порядке постановки).
pub fn load_messages(
    state: &CacheState,
    dialog_id: &str,
    limit: u32,
) -> Result<Vec<MessageView>, String> {
    let limit = if limit == 0 {
        CACHE_MSGS_PER_DIALOG as i64
    } else {
        (limit as i64).min(CACHE_MSGS_PER_DIALOG as i64)
    };

    let conn = state.conn()?;
    let mut stmt = conn
        .prepare(
            "SELECT id, dialog_id, direction, sender_type, sender_name, body_enc,
                    attachments_enc, delivery_status, client_message_id, created_at
             FROM cached_messages
             WHERE dialog_id = ?1
             ORDER BY created_at DESC, id DESC
             LIMIT ?2",
        )
        .map_err(|e| e.to_string())?;

    let rows = stmt
        .query_map(params![dialog_id, limit], |row| {
            Ok(MessageView {
                id: row.get(0)?,
                dialog_id: row.get(1)?,
                direction: row.get(2)?,
                sender_type: row.get(3)?,
                sender_name: row.get(4)?,
                body: state.crypto().decrypt_opt(row.get(5)?),
                attachments: state.crypto().decrypt_json(row.get(6)?),
                delivery_status: row.get(7)?,
                client_message_id: row.get(8)?,
                created_at: row.get(9)?,
                pending: false,
            })
        })
        .map_err(|e| e.to_string())?;

    let mut out = Vec::new();
    for r in rows {
        out.push(r.map_err(|e| e.to_string())?);
    }
    out.reverse(); // хронологический порядок для ленты

    // ⏳-строки: то, что ещё не подтверждено сервером.
    let mut stmt = conn
        .prepare(
            "SELECT client_msg_id, kind, body_enc, status, last_error, created_at
             FROM outbox WHERE dialog_id = ?1 ORDER BY id ASC",
        )
        .map_err(|e| e.to_string())?;
    let pend = stmt
        .query_map([dialog_id], |row| {
            let kind: String = row.get(1)?;
            let status: String = row.get(3)?;
            let cmid: String = row.get(0)?;
            Ok(MessageView {
                id: format!("outbox:{cmid}"),
                dialog_id: dialog_id.to_string(),
                direction: if kind == "note" { "note" } else { "out" }.to_string(),
                sender_type: "operator".to_string(),
                sender_name: None,
                body: state.crypto().decrypt_opt(row.get(2)?),
                attachments: None,
                delivery_status: if status == "failed" {
                    "failed"
                } else {
                    "pending"
                }
                .to_string(),
                client_message_id: Some(cmid),
                created_at: row.get(5)?,
                pending: true,
            })
        })
        .map_err(|e| e.to_string())?;
    for r in pend {
        out.push(r.map_err(|e| e.to_string())?);
    }

    Ok(out)
}

// ---------------------------------------------------------------------------
// Запись: «сервер прав»
// ---------------------------------------------------------------------------

/// Upsert диалогов + prune до [`CACHE_DIALOGS`]. Возвращает число записанных строк.
pub fn apply_sync(state: &CacheState, dialogs: &[DialogSync]) -> Result<usize, String> {
    if dialogs.is_empty() {
        return Ok(0);
    }
    let crypto = state.crypto();
    let now = now_iso();

    // Шифрование — до захвата соединения: держим лок минимально.
    struct Row {
        id: String,
        account_id: Option<String>,
        account_title: Option<String>,
        status: String,
        assignee_id: Option<String>,
        assignee_name: Option<String>,
        unread: i64,
        last_message_at: Option<String>,
        updated_at: Option<String>,
        name_enc: Option<Vec<u8>>,
        phone_enc: Option<Vec<u8>>,
        item_enc: Option<Vec<u8>>,
        card_enc: Vec<u8>,
    }

    let mut rows = Vec::with_capacity(dialogs.len());
    for d in dialogs {
        let card = serde_json::to_value(d).map_err(|e| e.to_string())?;
        rows.push(Row {
            id: d.id.clone(),
            account_id: d.account.as_ref().and_then(|a| a.id.clone()),
            account_title: d.account.as_ref().and_then(|a| a.title.clone()),
            status: d.status.clone().unwrap_or_else(|| "new".to_string()),
            assignee_id: d.assignee.as_ref().and_then(|a| a.id.clone()),
            assignee_name: d
                .assignee
                .as_ref()
                .and_then(|a| a.full_name.clone().or_else(|| a.name.clone())),
            unread: d.unread_count.unwrap_or(0),
            last_message_at: d.last_message_at.clone(),
            updated_at: d.updated_at.clone(),
            name_enc: crypto.encrypt_opt(
                d.client
                    .as_ref()
                    .and_then(|c| c.name.as_deref().or(c.full_name.as_deref())),
            )?,
            phone_enc: crypto.encrypt_opt(d.client.as_ref().and_then(|c| c.phone.as_deref()))?,
            item_enc: crypto.encrypt_opt(d.item.as_ref().and_then(|i| i.title.as_deref()))?,
            card_enc: crypto.encrypt_json(&card)?,
        });
    }

    let mut conn = state.conn()?;
    let tx = conn.transaction().map_err(|e| e.to_string())?;
    {
        let mut stmt = tx
            .prepare(
                "INSERT INTO dialogs (id, account_id, account_title, status,
                     assignee_id, assignee_name, unread, last_message_at, updated_at,
                     client_name_enc, client_phone_enc, item_title_enc, card_json_enc, synced_at)
                 VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14)
                 ON CONFLICT(id) DO UPDATE SET
                     account_id       = excluded.account_id,
                     account_title    = excluded.account_title,
                     status           = excluded.status,
                     assignee_id      = excluded.assignee_id,
                     assignee_name    = excluded.assignee_name,
                     unread           = excluded.unread,
                     last_message_at  = excluded.last_message_at,
                     updated_at       = excluded.updated_at,
                     client_name_enc  = excluded.client_name_enc,
                     client_phone_enc = excluded.client_phone_enc,
                     item_title_enc   = excluded.item_title_enc,
                     card_json_enc    = excluded.card_json_enc,
                     synced_at        = excluded.synced_at",
            )
            .map_err(|e| e.to_string())?;
        for r in &rows {
            stmt.execute(params![
                r.id,
                r.account_id,
                r.account_title,
                r.status,
                r.assignee_id,
                r.assignee_name,
                r.unread,
                r.last_message_at,
                r.updated_at,
                r.name_enc,
                r.phone_enc,
                r.item_enc,
                r.card_enc,
                now,
            ])
            .map_err(|e| e.to_string())?;
        }
        prune_dialogs(&tx)?;
    }
    tx.commit().map_err(|e| e.to_string())?;
    Ok(rows.len())
}

/// Upsert сообщений диалога + обрезка до [`CACHE_MSGS_PER_DIALOG`].
///
/// Побочный эффект: если у пришедшего сообщения есть `client_message_id`, совпавшая
/// строка outbox удаляется — это дедупликация ⏳ на случай «сервер принял, ответ
/// потерялся» (04 §5.4).
pub fn upsert_messages(
    state: &CacheState,
    dialog_id: &str,
    messages: &[MessageSync],
) -> Result<usize, String> {
    if messages.is_empty() {
        return Ok(0);
    }
    let crypto = state.crypto();

    struct Row {
        id: String,
        dialog_id: String,
        direction: String,
        sender_type: String,
        sender_name: Option<String>,
        body_enc: Option<Vec<u8>>,
        att_enc: Option<Vec<u8>>,
        delivery_status: String,
        client_message_id: Option<String>,
        created_at: String,
    }

    let mut rows = Vec::with_capacity(messages.len());
    for m in messages {
        let att_enc = match &m.attachments {
            Some(v) if !v.is_null() => Some(crypto.encrypt_json(v)?),
            _ => None,
        };
        rows.push(Row {
            id: m.id.clone(),
            dialog_id: m
                .conversation_id
                .clone()
                .unwrap_or_else(|| dialog_id.to_string()),
            direction: m.direction.clone().unwrap_or_else(|| "in".to_string()),
            sender_type: m
                .sender_type
                .clone()
                .unwrap_or_else(|| "system".to_string()),
            sender_name: m
                .sender
                .as_ref()
                .and_then(|s| s.full_name.clone().or_else(|| s.name.clone())),
            body_enc: crypto.encrypt_opt(m.body.as_deref())?,
            att_enc,
            delivery_status: m
                .delivery_status
                .clone()
                .unwrap_or_else(|| "delivered".to_string()),
            client_message_id: m.client_message_id.clone(),
            created_at: m.created_at.clone().unwrap_or_else(now_iso),
        });
    }

    let mut conn = state.conn()?;
    let tx = conn.transaction().map_err(|e| e.to_string())?;
    {
        // Сообщение может прийти раньше, чем диалог попал в кэш (WS быстрее синка):
        // ставим строку-заглушку, чтобы FK не отклонил вставку.
        let mut ensure = tx
            .prepare(
                "INSERT INTO dialogs (id, status, updated_at) VALUES (?1, 'new', ?2)
                 ON CONFLICT(id) DO NOTHING",
            )
            .map_err(|e| e.to_string())?;
        let mut stmt = tx
            .prepare(
                "INSERT INTO cached_messages (id, dialog_id, direction, sender_type,
                     sender_name, body_enc, attachments_enc, delivery_status,
                     client_message_id, created_at)
                 VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)
                 ON CONFLICT(id) DO UPDATE SET
                     dialog_id         = excluded.dialog_id,
                     direction         = excluded.direction,
                     sender_type       = excluded.sender_type,
                     sender_name       = excluded.sender_name,
                     body_enc          = excluded.body_enc,
                     attachments_enc   = excluded.attachments_enc,
                     delivery_status   = excluded.delivery_status,
                     client_message_id = excluded.client_message_id,
                     created_at        = excluded.created_at",
            )
            .map_err(|e| e.to_string())?;
        let mut dedup = tx
            .prepare("DELETE FROM outbox WHERE client_msg_id = ?1")
            .map_err(|e| e.to_string())?;

        let mut touched: Vec<String> = Vec::new();
        for r in &rows {
            ensure
                .execute(params![r.dialog_id, now_iso()])
                .map_err(|e| e.to_string())?;
            stmt.execute(params![
                r.id,
                r.dialog_id,
                r.direction,
                r.sender_type,
                r.sender_name,
                r.body_enc,
                r.att_enc,
                r.delivery_status,
                r.client_message_id,
                r.created_at,
            ])
            .map_err(|e| e.to_string())?;
            if let Some(cmid) = &r.client_message_id {
                dedup.execute([cmid]).map_err(|e| e.to_string())?;
            }
            if !touched.contains(&r.dialog_id) {
                touched.push(r.dialog_id.clone());
            }
        }

        let mut trim = tx
            .prepare(
                "DELETE FROM cached_messages
                 WHERE dialog_id = ?1 AND id NOT IN (
                     SELECT id FROM cached_messages WHERE dialog_id = ?1
                     ORDER BY created_at DESC, id DESC LIMIT ?2)",
            )
            .map_err(|e| e.to_string())?;
        for d in &touched {
            trim.execute(params![d, CACHE_MSGS_PER_DIALOG as i64])
                .map_err(|e| e.to_string())?;
        }
    }
    tx.commit().map_err(|e| e.to_string())?;
    Ok(rows.len())
}

/// Prune: оставить [`CACHE_DIALOGS`] самых свежих диалогов; сообщения выпавших
/// уходят каскадом (`PRAGMA foreign_keys=ON` включён при открытии соединения).
fn prune_dialogs(conn: &Connection) -> Result<usize, String> {
    let removed = conn
        .execute(
            "DELETE FROM dialogs WHERE id NOT IN (
                 SELECT id FROM dialogs
                 ORDER BY COALESCE(last_message_at, updated_at, '') DESC
                 LIMIT ?1)",
            params![CACHE_DIALOGS as i64],
        )
        .map_err(|e| e.to_string())?;
    // Подстраховка на случай, если каскад отключён (старый файл кэша).
    conn.execute(
        "DELETE FROM cached_messages
         WHERE dialog_id NOT IN (SELECT id FROM dialogs)",
        [],
    )
    .map_err(|e| e.to_string())?;
    Ok(removed)
}

/// Полная очистка кэша — вызывается при выходе из аккаунта.
///
/// Outbox тоже чистится: очередь принадлежит сессии, и оставлять чужие неотправленные
/// сообщения следующему пользователю на этой машине нельзя.
pub fn clear(state: &CacheState) -> Result<(), String> {
    let conn = state.conn()?;
    conn.execute_batch(
        "BEGIN;
         DELETE FROM cached_messages;
         DELETE FROM dialogs;
         DELETE FROM outbox;
         DELETE FROM meta;
         COMMIT;",
    )
    .map_err(|e| e.to_string())?;
    // Файл после массового DELETE не ужимается сам — просим SQLite вернуть страницы.
    conn.execute_batch("VACUUM; PRAGMA wal_checkpoint(TRUNCATE);")
        .ok();
    Ok(())
}

/// Есть ли диалог в кэше (нужно синку, чтобы не тянуть сообщения «в никуда»).
pub fn dialog_exists(state: &CacheState, dialog_id: &str) -> Result<bool, String> {
    let conn = state.conn()?;
    let found: Option<i64> = conn
        .query_row("SELECT 1 FROM dialogs WHERE id = ?1", [dialog_id], |r| {
            r.get(0)
        })
        .optional()
        .map_err(|e| e.to_string())?;
    Ok(found.is_some())
}

// ---------------------------------------------------------------------------
// Команды (04 §8.1)
// ---------------------------------------------------------------------------

/// Расшифрованный список диалогов для мгновенного старта.
#[tauri::command]
pub async fn cache_load_dialogs(
    state: tauri::State<'_, CacheState>,
) -> Result<Vec<DialogView>, String> {
    load_dialogs(&state)
}

/// Upsert серверных данных в кэш (шифрование колонок внутри) + prune 200/100.
///
/// `messages` опционален: старт/дельта присылают только диалоги, открытие диалога —
/// диалоги и его ленту.
#[tauri::command]
pub async fn cache_apply_sync(
    state: tauri::State<'_, CacheState>,
    dialogs: Vec<DialogSync>,
    messages: Option<Vec<MessageSync>>,
) -> Result<(), String> {
    apply_sync(&state, &dialogs)?;
    if let Some(msgs) = messages {
        // Группируем по conversation_id: одним вызовом может прийти лента нескольких
        // диалогов (догон после reconnect).
        let mut by_dialog: std::collections::HashMap<String, Vec<MessageSync>> =
            std::collections::HashMap::new();
        for m in msgs {
            let key = m
                .conversation_id
                .clone()
                .or_else(|| dialogs.first().map(|d| d.id.clone()))
                .unwrap_or_default();
            if key.is_empty() {
                continue;
            }
            by_dialog.entry(key).or_default().push(m);
        }
        for (dialog_id, group) in by_dialog {
            upsert_messages(&state, &dialog_id, &group)?;
        }
    }
    Ok(())
}

/// Положить в кэш последние сообщения диалога (обрезка до 100 внутри).
#[tauri::command]
pub async fn cache_upsert_messages(
    state: tauri::State<'_, CacheState>,
    dialog_id: String,
    messages: Vec<MessageSync>,
) -> Result<(), String> {
    upsert_messages(&state, &dialog_id, &messages)?;
    Ok(())
}

/// Расшифрованные сообщения диалога (+ ⏳-строки из outbox).
#[tauri::command]
pub async fn cache_load_messages(
    state: tauri::State<'_, CacheState>,
    dialog_id: String,
    limit: u32,
) -> Result<Vec<MessageView>, String> {
    load_messages(&state, &dialog_id, limit)
}

/// Очистить кэш (выход из аккаунта).
#[tauri::command]
pub async fn cache_clear(state: tauri::State<'_, CacheState>) -> Result<(), String> {
    state.set_session_token(None);
    clear(&state)
}

/// Суммарное число непрочитанных по кэшу — для бейджа в офлайне.
#[tauri::command]
pub async fn cache_unread_total(state: tauri::State<'_, CacheState>) -> Result<i64, String> {
    unread_total(&state)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cache::tests::test_state;

    fn dialog(id: &str, last: &str, name: &str) -> DialogSync {
        serde_json::from_value(serde_json::json!({
            "id": id,
            "status": "in_progress",
            "account": {"id": "acc-1", "title": "LP-Москва"},
            "client": {"id": "cl-1", "name": name, "phone": "+79261234567"},
            "assignee": {"id": "u-1", "full_name": "Анна Смирнова"},
            "item": {"title": "Ремонт iPhone 13"},
            "unread_count": 2,
            "tags": ["негатив"],
            "last_message_at": last,
            "updated_at": last
        }))
        .unwrap()
    }

    fn message(id: &str, conv: &str, body: &str, created: &str) -> MessageSync {
        serde_json::from_value(serde_json::json!({
            "id": id,
            "conversation_id": conv,
            "direction": "in",
            "sender_type": "client",
            "body": body,
            "attachments": [],
            "delivery_status": "delivered",
            "created_at": created
        }))
        .unwrap()
    }

    #[test]
    fn upsert_and_read_back_decrypted() {
        let st = test_state();
        apply_sync(&st, &[dialog("d1", "2026-08-05T10:00:00Z", "Иван Петров")]).unwrap();
        let list = load_dialogs(&st).unwrap();
        assert_eq!(list.len(), 1);
        assert_eq!(list[0].client_name.as_deref(), Some("Иван Петров"));
        assert_eq!(list[0].client_phone.as_deref(), Some("+79261234567"));
        assert_eq!(list[0].item_title.as_deref(), Some("Ремонт iPhone 13"));
        assert_eq!(list[0].account_title.as_deref(), Some("LP-Москва"));
        assert_eq!(list[0].unread, 2);
        // карточка сохранена целиком, включая поля, которых нет в колонках
        let card = list[0].card.as_ref().unwrap();
        assert_eq!(card["tags"][0], "негатив");
    }

    #[test]
    fn ciphertext_is_actually_on_disk() {
        let st = test_state();
        apply_sync(&st, &[dialog("d1", "2026-08-05T10:00:00Z", "Иван Петров")]).unwrap();
        let conn = st.conn().unwrap();
        let blob: Vec<u8> = conn
            .query_row(
                "SELECT client_name_enc FROM dialogs WHERE id='d1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(
            !String::from_utf8_lossy(&blob).contains("Иван"),
            "имя клиента не должно лежать открытым текстом"
        );
    }

    #[test]
    fn server_wins_on_conflict() {
        let st = test_state();
        apply_sync(&st, &[dialog("d1", "2026-08-05T10:00:00Z", "Старое имя")]).unwrap();
        let mut updated = dialog("d1", "2026-08-05T12:00:00Z", "Новое имя");
        updated.unread_count = Some(0);
        apply_sync(&st, &[updated]).unwrap();
        let list = load_dialogs(&st).unwrap();
        assert_eq!(list.len(), 1, "upsert, а не вставка дубля");
        assert_eq!(list[0].client_name.as_deref(), Some("Новое имя"));
        assert_eq!(list[0].unread, 0);
    }

    #[test]
    fn prune_keeps_200_freshest() {
        let st = test_state();
        let batch: Vec<DialogSync> = (0..CACHE_DIALOGS + 25)
            .map(|i| {
                dialog(
                    &format!("d{i:04}"),
                    &crate::cache::format_iso(1_700_000_000 + i as i64 * 60),
                    "Клиент",
                )
            })
            .collect();
        apply_sync(&st, &batch).unwrap();
        let list = load_dialogs(&st).unwrap();
        assert_eq!(list.len(), CACHE_DIALOGS);
        assert_eq!(
            list[0].id,
            format!("d{:04}", CACHE_DIALOGS + 24),
            "свежий сверху"
        );
        assert!(!list.iter().any(|d| d.id == "d0000"), "старые вытеснены");
    }

    #[test]
    fn messages_trimmed_to_100_and_ordered() {
        let st = test_state();
        apply_sync(&st, &[dialog("d1", "2026-08-05T10:00:00Z", "Иван")]).unwrap();
        let msgs: Vec<MessageSync> = (0..CACHE_MSGS_PER_DIALOG + 30)
            .map(|i| {
                message(
                    &format!("m{i:04}"),
                    "d1",
                    &format!("текст {i}"),
                    &crate::cache::format_iso(1_700_000_000 + i as i64),
                )
            })
            .collect();
        upsert_messages(&st, "d1", &msgs).unwrap();

        let conn_count: i64 = st
            .conn()
            .unwrap()
            .query_row("SELECT COUNT(*) FROM cached_messages", [], |r| r.get(0))
            .unwrap();
        assert_eq!(conn_count, CACHE_MSGS_PER_DIALOG as i64);

        let view = load_messages(&st, "d1", 100).unwrap();
        assert_eq!(view.len(), CACHE_MSGS_PER_DIALOG);
        assert!(
            view[0].created_at < view[view.len() - 1].created_at,
            "хронология"
        );
        assert_eq!(view[view.len() - 1].body.as_deref(), Some("текст 129"));
    }

    #[test]
    fn prune_cascades_to_messages() {
        let st = test_state();
        apply_sync(&st, &[dialog("old", "2020-01-01T00:00:00Z", "Клиент")]).unwrap();
        upsert_messages(
            &st,
            "old",
            &[message("m1", "old", "привет", "2020-01-01T00:00:00Z")],
        )
        .unwrap();
        let batch: Vec<DialogSync> = (0..CACHE_DIALOGS)
            .map(|i| {
                dialog(
                    &format!("n{i:04}"),
                    &crate::cache::format_iso(1_800_000_000 + i as i64),
                    "Клиент",
                )
            })
            .collect();
        apply_sync(&st, &batch).unwrap();
        let left: i64 = st
            .conn()
            .unwrap()
            .query_row(
                "SELECT COUNT(*) FROM cached_messages WHERE dialog_id='old'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(left, 0, "сообщения вытесненного диалога уходят каскадом");
    }

    #[test]
    fn message_for_unknown_dialog_does_not_fail() {
        let st = test_state();
        upsert_messages(
            &st,
            "ghost",
            &[message("m1", "ghost", "текст", "2026-08-05T10:00:00Z")],
        )
        .unwrap();
        assert!(dialog_exists(&st, "ghost").unwrap());
    }

    #[test]
    fn clear_wipes_everything() {
        let st = test_state();
        apply_sync(&st, &[dialog("d1", "2026-08-05T10:00:00Z", "Иван")]).unwrap();
        upsert_messages(
            &st,
            "d1",
            &[message("m1", "d1", "текст", "2026-08-05T10:00:00Z")],
        )
        .unwrap();
        st.meta_set("last_sync_at", "2026-08-05T10:00:00Z").unwrap();
        clear(&st).unwrap();
        assert!(load_dialogs(&st).unwrap().is_empty());
        assert!(load_messages(&st, "d1", 100).unwrap().is_empty());
        assert_eq!(st.meta_get("last_sync_at").unwrap(), None);
    }

    #[test]
    fn unread_total_sums_cache() {
        let st = test_state();
        apply_sync(
            &st,
            &[
                dialog("d1", "2026-08-05T10:00:00Z", "Иван"),
                dialog("d2", "2026-08-05T11:00:00Z", "Пётр"),
            ],
        )
        .unwrap();
        assert_eq!(unread_total(&st).unwrap(), 4);
    }
}
