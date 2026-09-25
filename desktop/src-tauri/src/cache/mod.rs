//! Локальный офлайн-кэш десктопа (04-DESKTOP §5).
//!
//! Состав модуля:
//! * [`crypto`] — AES-256-GCM поверх мастер-ключа (шифрование содержательных колонок);
//! * [`dpapi`] — защита мастер-ключа на диске (DPAPI на Windows, dev-заглушка иначе);
//! * [`store`] — dialogs / cached_messages: upsert «сервер прав», чтение, prune;
//! * [`outbox`] — исходящая очередь: push / flush / retry / delete.
//!
//! Всё содержательное шифруется; расшифрованный мастер-ключ живёт ТОЛЬКО в памяти
//! Rust-процесса и в JS не отдаётся никогда (04 §5.5).

pub mod crypto;
pub mod dpapi;
pub mod outbox;
pub mod store;

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, MutexGuard, RwLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use rusqlite::Connection;
use tauri::{AppHandle, Manager};

pub use crypto::{Crypto, KEY_LEN};
pub use outbox::{FailedItem, FlushReport, OutboxItemView};
pub use store::{DialogSync, DialogView, MessageSync, MessageView};

/// База API по умолчанию (04 §1.1). Переопределяется переменной окружения
/// `LEADCHAT_API_BASE` или командой `set_api_base` (стенды, self-hosted).
pub const DEFAULT_API_BASE: &str = "https://chat.partner-lead-centre.ru";

/// Имя файла кэша в `app_data_dir`.
pub const DB_FILE: &str = "cache.db";
/// Имя файла с защищённым мастер-ключом.
pub const KEY_FILE: &str = "cache.key";

/// Таймаут HTTP-запросов, которые Rust делает сам (outbox, синхронизация).
pub const HTTP_TIMEOUT_SECS: u64 = 15;

// ---------------------------------------------------------------------------
// Миграции
// ---------------------------------------------------------------------------

/// Одна миграция локального кэша. Собственный тип, чтобы модуль кэша не зависел
/// от конкретного плагина (см. cross-boundary note к `lib.rs`).
#[derive(Debug, Clone, Copy)]
pub struct CacheMigration {
    pub version: i64,
    pub description: &'static str,
    pub sql: &'static str,
}

/// Список миграций кэша в порядке применения.
///
/// Применяются автоматически в [`CacheState::open`] (rusqlite). Регистрировать их
/// дополнительно в `tauri-plugin-sql` не обязательно; если это всё же нужно —
/// см. mapping-сниппет в cross-boundary note (SQL написан идемпотентно,
/// `CREATE TABLE IF NOT EXISTS`, поэтому двойное применение безопасно).
pub fn migrations() -> Vec<CacheMigration> {
    vec![CacheMigration {
        version: 1,
        description: "init cache schema",
        sql: include_str!("../../migrations/0001_init.sql"),
    }]
}

// ---------------------------------------------------------------------------
// Время: ISO-8601 UTC без внешних зависимостей
// ---------------------------------------------------------------------------

/// Текущее время в формате `YYYY-MM-DDTHH:MM:SSZ` (01-API-SPEC §1.5).
pub fn now_iso() -> String {
    format_iso(now_epoch())
}

/// Текущее время в секундах с эпохи.
pub fn now_epoch() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Секунды с эпохи → `YYYY-MM-DDTHH:MM:SSZ`.
///
/// Формат фиксированной ширины: строки такого вида сравниваются лексикографически
/// в том же порядке, что и хронологически, — на этом держатся `ORDER BY` и
/// `next_try_at <= now` в SQL без парсинга дат.
pub fn format_iso(epoch: i64) -> String {
    let days = epoch.div_euclid(86_400);
    let secs = epoch.rem_euclid(86_400);
    let (y, m, d) = civil_from_days(days);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z",
        secs / 3600,
        (secs % 3600) / 60,
        secs % 60
    )
}

/// `YYYY-MM-DDTHH:MM:SS[.fff][Z|+hh:mm]` → секунды с эпохи.
///
/// Разбираем ровно то, что отдаёт бэкенд (01 §1.5 — всегда UTC с `Z`); дробную часть
/// отбрасываем, смещение, если оно вдруг придёт, учитываем.
pub fn parse_iso(s: &str) -> Option<i64> {
    let b = s.as_bytes();
    if b.len() < 19 || b[4] != b'-' || b[7] != b'-' || (b[10] != b'T' && b[10] != b' ') {
        return None;
    }
    let num = |from: usize, to: usize| -> Option<i64> { s.get(from..to)?.parse::<i64>().ok() };
    let (y, mo, d) = (num(0, 4)?, num(5, 7)?, num(8, 10)?);
    let (h, mi, sec) = (num(11, 13)?, num(14, 16)?, num(17, 19)?);
    if !(1..=12).contains(&mo) || !(1..=31).contains(&d) {
        return None;
    }
    let mut epoch = days_from_civil(y, mo as u32, d as u32) * 86_400 + h * 3600 + mi * 60 + sec;

    // Хвост: пропускаем дробные секунды, затем смотрим на смещение.
    let mut rest = &s[19..];
    if rest.starts_with('.') {
        let cut = rest[1..]
            .find(|c: char| !c.is_ascii_digit())
            .map(|i| i + 1)
            .unwrap_or(rest.len());
        rest = &rest[cut..];
    }
    if let Some(sign) = rest.chars().next() {
        if sign == '+' || sign == '-' {
            let off = &rest[1..];
            if off.len() >= 5 {
                let oh: i64 = off[0..2].parse().ok()?;
                let om: i64 = off[3..5].parse().ok()?;
                let delta = oh * 3600 + om * 60;
                epoch += if sign == '+' { -delta } else { delta };
            }
        }
    }
    Some(epoch)
}

/// Дни с 1970-01-01 (алгоритм Говарда Хиннанта, `days_from_civil`).
fn days_from_civil(y: i64, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400; // [0, 399]
    let m = m as i64;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d as i64 - 1; // [0, 365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// Обратное преобразование (`civil_from_days`).
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

// ---------------------------------------------------------------------------
// CacheState
// ---------------------------------------------------------------------------

/// Разделяемое состояние кэша: соединение с SQLite, шифр, access-JWT и HTTP-клиент.
///
/// Кладётся в `app.manage(...)` из [`init`]; команды получают его как
/// `tauri::State<'_, CacheState>`.
pub struct CacheState {
    conn: Mutex<Connection>,
    crypto: Crypto,
    /// access-JWT текущей сессии. Только в памяти; на диск не пишется (04 §5.5).
    session_token: RwLock<Option<String>>,
    api_base: RwLock<String>,
    http: reqwest::Client,
    /// Защита от параллельных flush (таймер и reconnect могут совпасть).
    flushing: AtomicBool,
    db_path: PathBuf,
}

impl CacheState {
    /// Открыть (создав при необходимости) файл кэша и применить миграции.
    pub fn open(db_path: PathBuf, master: [u8; KEY_LEN]) -> Result<Self, String> {
        if let Some(dir) = db_path.parent() {
            std::fs::create_dir_all(dir).map_err(|e| format!("{dir:?}: {e}"))?;
        }
        let conn = Connection::open(&db_path).map_err(|e| format!("open {db_path:?}: {e}"))?;

        // WAL нельзя включать внутри транзакции — поэтому не в миграции, а здесь.
        let _: String = conn
            .query_row("PRAGMA journal_mode=WAL", [], |r| r.get(0))
            .map_err(|e| format!("PRAGMA journal_mode: {e}"))?;
        conn.execute_batch(
            "PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000; PRAGMA synchronous=NORMAL;",
        )
        .map_err(|e| format!("PRAGMA: {e}"))?;

        apply_migrations(&conn)?;

        // Восстановление после падения/убийства процесса: строки, застрявшие в
        // 'sending', возвращаем в очередь — идемпотентность по client_message_id
        // (01 §1.6) гарантирует, что повторная отправка не создаст дубль.
        conn.execute(
            "UPDATE outbox SET status='pending' WHERE status='sending'",
            [],
        )
        .map_err(|e| format!("outbox recover: {e}"))?;

        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(HTTP_TIMEOUT_SECS))
            .user_agent(concat!("LeadChat-Desktop/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(|e| format!("http client: {e}"))?;

        let api_base = std::env::var("LEADCHAT_API_BASE")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .unwrap_or_else(|| DEFAULT_API_BASE.to_string());

        Ok(Self {
            conn: Mutex::new(conn),
            crypto: Crypto::new(&master),
            session_token: RwLock::new(None),
            api_base: RwLock::new(api_base.trim_end_matches('/').to_string()),
            http,
            flushing: AtomicBool::new(false),
            db_path,
        })
    }

    /// Соединение с БД.
    ///
    /// ВНИМАНИЕ: guard нельзя держать через `.await` (иначе future перестанет быть
    /// `Send` и `#[tauri::command] async fn` не скомпилируется). Во всех async-командах
    /// работа с БД делается в блоке, который закрывается ДО первого await.
    pub fn conn(&self) -> Result<MutexGuard<'_, Connection>, String> {
        self.conn
            .lock()
            .map_err(|_| "cache: db mutex poisoned".to_string())
    }

    /// Шифр кэша.
    pub fn crypto(&self) -> &Crypto {
        &self.crypto
    }

    /// HTTP-клиент с общим таймаутом.
    pub fn http(&self) -> &reqwest::Client {
        &self.http
    }

    /// Текущая база API без завершающего слэша.
    pub fn api_base(&self) -> String {
        self.api_base
            .read()
            .map(|v| v.clone())
            .unwrap_or_else(|_| DEFAULT_API_BASE.to_string())
    }

    /// Сменить базу API (стенды/self-hosted).
    pub fn set_api_base(&self, base: &str) {
        if let Ok(mut w) = self.api_base.write() {
            *w = base.trim().trim_end_matches('/').to_string();
        }
    }

    /// access-JWT текущей сессии (пришедший от фронта).
    pub fn session_token(&self) -> Result<String, String> {
        self.session_token
            .read()
            .map_err(|_| "cache: token lock poisoned".to_string())?
            .clone()
            .ok_or_else(|| "no_session_token".to_string())
    }

    /// Запомнить access-JWT (только в памяти процесса).
    pub fn set_session_token(&self, token: Option<String>) {
        if let Ok(mut w) = self.session_token.write() {
            *w = token.filter(|t| !t.trim().is_empty());
        }
    }

    /// Взять flush-лок. `None`, если flush уже идёт.
    pub(crate) fn try_flush_guard(&self) -> Option<FlushGuard<'_>> {
        if self.flushing.swap(true, Ordering::SeqCst) {
            None
        } else {
            Some(FlushGuard {
                flag: &self.flushing,
            })
        }
    }

    /// Путь к файлу кэша (для диагностики и пересоздания).
    pub fn db_path(&self) -> &Path {
        &self.db_path
    }

    // --- meta -------------------------------------------------------------

    /// Прочитать значение из таблицы `meta`.
    pub fn meta_get(&self, key: &str) -> Result<Option<String>, String> {
        let conn = self.conn()?;
        let mut stmt = conn
            .prepare("SELECT value FROM meta WHERE key = ?1")
            .map_err(|e| e.to_string())?;
        let mut rows = stmt.query([key]).map_err(|e| e.to_string())?;
        match rows.next().map_err(|e| e.to_string())? {
            Some(row) => Ok(Some(row.get(0).map_err(|e| e.to_string())?)),
            None => Ok(None),
        }
    }

    /// Записать значение в таблицу `meta`.
    pub fn meta_set(&self, key: &str, value: &str) -> Result<(), String> {
        let conn = self.conn()?;
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?1, ?2)
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            rusqlite::params![key, value],
        )
        .map_err(|e| e.to_string())?;
        Ok(())
    }
}

/// RAII-гард flush'а. Отдельным типом (а не `MutexGuard`), чтобы future оставался `Send`.
pub(crate) struct FlushGuard<'a> {
    flag: &'a AtomicBool,
}

impl Drop for FlushGuard<'_> {
    fn drop(&mut self) {
        self.flag.store(false, Ordering::SeqCst);
    }
}

/// Прогнать миграции с версионированием в таблице `schema_migrations`.
fn apply_migrations(conn: &Connection) -> Result<(), String> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
         );",
    )
    .map_err(|e| format!("schema_migrations: {e}"))?;

    for m in migrations() {
        let done: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = ?1",
                [m.version],
                |r| r.get(0),
            )
            .map_err(|e| e.to_string())?;
        if done > 0 {
            continue;
        }
        conn.execute_batch(&format!("BEGIN; {} COMMIT;", m.sql))
            .map_err(|e| format!("migration {} ({}): {e}", m.version, m.description))?;
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES(?1, ?2)",
            rusqlite::params![m.version, now_iso()],
        )
        .map_err(|e| e.to_string())?;
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Инициализация при старте (04 §5.5)
// ---------------------------------------------------------------------------

/// Поднять кэш: развернуть мастер-ключ, открыть БД, положить состояние в `app.manage`.
///
/// Вызывается из `lib.rs` в `setup(...)`: `crate::cache::init(app.handle())?;`
pub fn init(app: &AppHandle) -> Result<(), String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("app_data_dir: {e}"))?;
    std::fs::create_dir_all(&dir).map_err(|e| format!("{dir:?}: {e}"))?;

    let key_path = dir.join(KEY_FILE);
    let db_path = dir.join(DB_FILE);

    let master: [u8; KEY_LEN] = if key_path.exists() {
        match dpapi::read_protected(&key_path) {
            Ok(k) => match <[u8; KEY_LEN]>::try_from(k.as_slice()) {
                Ok(k) => k,
                Err(_) => {
                    eprintln!("[leadchat][cache] cache.key повреждён — пересоздаём кэш");
                    reset_cache(&db_path);
                    new_master_key(&key_path)?
                }
            },
            Err(e) => {
                // Профиль Windows переехал/переустановлен, либо файл принесли с чужой
                // машины → развернуть нельзя. Кэш — всего лишь проекция сервера,
                // поэтому молча пересоздаём с нуля (04 §5.5).
                eprintln!("[leadchat][cache] cache.key не разворачивается ({e}) — пересоздаём кэш");
                reset_cache(&db_path);
                new_master_key(&key_path)?
            }
        }
    } else {
        new_master_key(&key_path)?
    };

    let state = CacheState::open(db_path, master)?;
    app.manage(state);
    Ok(())
}

/// Сгенерировать новый мастер-ключ и положить его на диск под защитой ОС.
fn new_master_key(path: &Path) -> Result<[u8; KEY_LEN], String> {
    use rand::RngCore;
    let mut key = [0u8; KEY_LEN];
    rand::rngs::OsRng.fill_bytes(&mut key);
    dpapi::write_protected(path, &key)?;
    Ok(key)
}

/// Удалить файлы БД (включая WAL/SHM) — используется, когда ключ не разворачивается.
fn reset_cache(db_path: &Path) {
    for suffix in ["", "-wal", "-shm"] {
        let p = if suffix.is_empty() {
            db_path.to_path_buf()
        } else {
            PathBuf::from(format!("{}{suffix}", db_path.display()))
        };
        std::fs::remove_file(p).ok();
    }
}

// ---------------------------------------------------------------------------
// Команды
// ---------------------------------------------------------------------------

/// Принять от фронта текущий access-JWT для Rust-запросов (`outbox_flush`, синк).
///
/// Токен живёт только в памяти процесса (04 §5.5). Пустая строка = «сессии нет»
/// (например, после логаута).
#[tauri::command]
pub fn set_session_token(state: tauri::State<'_, CacheState>, token: String) -> Result<(), String> {
    state.set_session_token(Some(token));
    Ok(())
}

/// Сменить базу API в рантайме (стенды/self-hosted). Опционально для фронта.
#[tauri::command]
pub fn set_api_base(state: tauri::State<'_, CacheState>, base: String) -> Result<(), String> {
    if !(base.starts_with("https://") || base.starts_with("http://")) {
        return Err("api_base должен начинаться с http:// или https://".to_string());
    }
    state.set_api_base(&base);
    Ok(())
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    use std::io::{Read, Write};
    use std::sync::atomic::AtomicUsize;
    use std::sync::Arc;

    /// Тестовый кэш во временном каталоге. Используется и тестами `sync.rs`.
    pub(crate) fn test_state() -> CacheState {
        let dir = std::env::temp_dir().join(format!(
            "lc-cache-{}-{}",
            std::process::id(),
            now_epoch() as u64 + rand::random::<u16>() as u64
        ));
        std::fs::create_dir_all(&dir).unwrap();
        CacheState::open(dir.join(DB_FILE), [3u8; KEY_LEN]).unwrap()
    }

    /// Кэш, готовый ходить в сеть: база API — мок-сервер, токен в памяти.
    pub(crate) fn ready_state(base: &str) -> CacheState {
        let st = test_state();
        st.set_api_base(base);
        st.set_session_token(Some("jwt-access".into()));
        st
    }

    /// Приёмник событий вместо `AppHandle` — считает вызовы.
    #[derive(Default)]
    pub(crate) struct TestEvents {
        pub refresh: AtomicUsize,
        pub reports: AtomicUsize,
    }

    impl outbox::FlushEvents for TestEvents {
        fn session_refresh_needed(&self) {
            self.refresh.fetch_add(1, Ordering::SeqCst);
        }
        fn report(&self, _report: &outbox::FlushReport) {
            self.reports.fetch_add(1, Ordering::SeqCst);
        }
    }

    /// Мок-сервер: отвечает по сценарию `(код, тело)` и записывает полученные запросы.
    pub(crate) fn mock_server(script: Vec<(u16, String)>) -> (String, Arc<Mutex<Vec<String>>>) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let seen: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
        let sink = seen.clone();

        std::thread::spawn(move || {
            for (i, conn) in listener.incoming().enumerate() {
                let Ok(mut stream) = conn else { break };
                let req = read_request(&mut stream);
                sink.lock().unwrap().push(req);
                let (code, body) = script
                    .get(i)
                    .cloned()
                    .unwrap_or((200, "{\"items\":[]}".to_string()));
                let resp = format!(
                    "HTTP/1.1 {code} STATUS\r\nContent-Type: application/json\r\n\
                     Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(resp.as_bytes());
                let _ = stream.flush();
            }
        });
        (format!("http://127.0.0.1:{port}"), seen)
    }

    /// Прочитать HTTP-запрос целиком: заголовки + тело по `Content-Length`.
    fn read_request(stream: &mut std::net::TcpStream) -> String {
        let mut buf = Vec::new();
        let mut chunk = [0u8; 1024];
        loop {
            let n = match stream.read(&mut chunk) {
                Ok(0) | Err(_) => break,
                Ok(n) => n,
            };
            buf.extend_from_slice(&chunk[..n]);
            let text = String::from_utf8_lossy(&buf).to_string();
            if let Some(head_end) = text.find("\r\n\r\n") {
                let len = text
                    .lines()
                    .find_map(|l| {
                        let (k, v) = l.split_once(':')?;
                        k.eq_ignore_ascii_case("content-length")
                            .then(|| v.trim().parse::<usize>().ok())?
                    })
                    .unwrap_or(0);
                if buf.len() >= head_end + 4 + len {
                    break;
                }
            }
        }
        String::from_utf8_lossy(&buf).to_string()
    }

    /// Порт, на котором заведомо никто не слушает (эмуляция «выдернули сеть»).
    pub(crate) fn dead_port() -> u16 {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let p = l.local_addr().unwrap().port();
        drop(l);
        p
    }

    #[test]
    fn iso_roundtrip() {
        // Эталоны сверены с `date -u -r <epoch>`.
        assert_eq!(format_iso(0), "1970-01-01T00:00:00Z");
        assert_eq!(format_iso(951_782_400), "2000-02-29T00:00:00Z"); // високосный год
        assert_eq!(format_iso(1_754_390_463), "2025-08-05T10:41:03Z");
        assert_eq!(format_iso(2_000_000_000), "2033-05-18T03:33:20Z");
        for e in [0i64, 1, 951_782_400, 1_754_390_463, 2_000_000_000] {
            assert_eq!(parse_iso(&format_iso(e)), Some(e), "epoch {e}");
        }
    }

    #[test]
    fn iso_parses_server_formats() {
        assert_eq!(
            parse_iso("2026-08-04T09:41:03.512Z"),
            parse_iso("2026-08-04T09:41:03Z")
        );
        assert!(parse_iso("2026-08-04").is_none());
        assert!(parse_iso("мусор").is_none());
    }

    #[test]
    fn iso_strings_sort_chronologically() {
        let mut v = [format_iso(3000), format_iso(10), format_iso(1_700_000_000)];
        v.sort();
        assert_eq!(v[0], format_iso(10));
        assert_eq!(v[2], format_iso(1_700_000_000));
    }

    #[test]
    fn open_applies_migrations_and_is_idempotent() {
        let st = test_state();
        let path = st.db_path().to_path_buf();
        {
            let conn = st.conn().unwrap();
            let n: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN
                     ('meta','dialogs','cached_messages','outbox')",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(n, 4);
        }
        drop(st);
        // Повторное открытие того же файла не должно падать.
        CacheState::open(path, [3u8; KEY_LEN]).unwrap();
    }

    #[test]
    fn meta_roundtrip() {
        let st = test_state();
        assert_eq!(st.meta_get("last_sync_at").unwrap(), None);
        st.meta_set("last_sync_at", "2026-08-05T10:00:00Z").unwrap();
        st.meta_set("last_sync_at", "2026-08-05T11:00:00Z").unwrap();
        assert_eq!(
            st.meta_get("last_sync_at").unwrap().as_deref(),
            Some("2026-08-05T11:00:00Z")
        );
    }

    #[test]
    fn session_token_is_memory_only() {
        let st = test_state();
        assert!(st.session_token().is_err());
        st.set_session_token(Some("jwt-1".into()));
        assert_eq!(st.session_token().unwrap(), "jwt-1");
        st.set_session_token(Some("   ".into()));
        assert!(st.session_token().is_err(), "пустой токен = нет сессии");
    }

    #[test]
    fn flush_guard_is_exclusive() {
        let st = test_state();
        let g = st.try_flush_guard();
        assert!(g.is_some());
        assert!(st.try_flush_guard().is_none());
        drop(g);
        assert!(st.try_flush_guard().is_some());
    }
}
