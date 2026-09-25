-- LeadChat desktop — локальный кэш (04-DESKTOP §5.2).
--
-- Принципы (04 §5.1):
--   * кэш — проекция сервера, ТОЛЬКО ДЛЯ ЧТЕНИЯ; единственные локально-авторитетные
--     данные — очередь `outbox`;
--   * при любом расхождении локальная строка перезаписывается серверной (сервер прав);
--   * всё содержательное (тексты, имена, телефоны, карточка) лежит в колонках `*_enc`
--     как AES-256-GCM (nonce(12) || ciphertext), ключ — только в памяти Rust-процесса.
--
-- ВАЖНО: `PRAGMA journal_mode = WAL` здесь НЕ ставится — pragma нельзя выполнить внутри
-- транзакции, а раннеры миграций (и rusqlite-раннер в cache/mod.rs, и tauri-plugin-sql)
-- оборачивают миграцию в транзакцию. WAL/foreign_keys/busy_timeout выставляются при
-- открытии соединения (cache::CacheState::open).

-- служебное состояние: last_sync_at | user_id | tray_hint_shown | ...
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- проекция conversations сервера (01-API-SPEC §5.1)
CREATE TABLE IF NOT EXISTS dialogs (
  id               TEXT PRIMARY KEY,      -- conversations.id (uuid) с сервера
  account_id       TEXT,                  -- avito_accounts.id
  account_title    TEXT,                  -- «LP-Москва» — для списка без JOIN'ов
  status           TEXT NOT NULL DEFAULT 'new',   -- new | in_progress | closed
  assignee_id      TEXT,
  assignee_name    TEXT,
  unread           INTEGER NOT NULL DEFAULT 0,
  last_message_at  TEXT,                  -- ISO-8601 UTC
  updated_at       TEXT,                  -- серверный updated_at — курсор дельта-синка
  client_name_enc  BLOB,                  -- AES-256-GCM (nonce || ciphertext)
  client_phone_enc BLOB,
  item_title_enc   BLOB,
  card_json_enc    BLOB,                  -- полный JSON элемента списка (клиент, объявление, теги)
  synced_at        TEXT                   -- когда строка последний раз пришла с сервера
);
CREATE INDEX IF NOT EXISTS idx_dialogs_lastmsg ON dialogs(last_message_at DESC);
CREATE INDEX IF NOT EXISTS idx_dialogs_updated ON dialogs(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_dialogs_unread  ON dialogs(unread);

-- проекция messages сервера (01-API-SPEC §6.1), последние 100 на диалог
CREATE TABLE IF NOT EXISTS cached_messages (
  id                TEXT PRIMARY KEY,     -- messages.id с сервера
  dialog_id         TEXT NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
  direction         TEXT NOT NULL DEFAULT 'in',      -- in | out | note | system
  sender_type       TEXT NOT NULL DEFAULT 'system',  -- client | operator | bot | system
  sender_name       TEXT,
  body_enc          BLOB,
  attachments_enc   BLOB,                 -- JSON-метаданные вложений (файлы не кэшируем)
  delivery_status   TEXT NOT NULL DEFAULT 'delivered',
  client_message_id TEXT,                 -- 01 §1.6 — дедупликация ⏳-строк outbox
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cmsgs_dialog ON cached_messages(dialog_id, created_at);
CREATE INDEX IF NOT EXISTS idx_cmsgs_cmid   ON cached_messages(client_message_id);

-- исходящая очередь — единственные локально-авторитетные данные (04 §5.3)
CREATE TABLE IF NOT EXISTS outbox (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  client_msg_id TEXT NOT NULL UNIQUE,     -- uuid v4, генерируется при постановке в очередь
  dialog_id     TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT 'message'
                CHECK (kind IN ('message', 'note')),
  body_enc      BLOB NOT NULL,            -- AES-256-GCM (nonce || ciphertext)
  status        TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'sending', 'failed')),
  attempts      INTEGER NOT NULL DEFAULT 0,
  next_try_at   TEXT,                     -- экспоненциальный backoff, ISO-8601 UTC
  last_error    TEXT,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_due    ON outbox(status, next_try_at, id);
CREATE INDEX IF NOT EXISTS idx_outbox_dialog ON outbox(dialog_id, created_at);
