//! Автообновление (04-DESKTOP §6, экран баннера — 11-SCREENS §7.4).
//!
//! Главное правило, из-за которого модуль вообще существует отдельно от плагина:
//! **обновление никогда не ставится без явного клика оператора.** У человека может
//! быть открыт недописанный ответ клиенту; молча перезапустить приложение —
//! потерять его работу. Поэтому здесь только два самостоятельных действия:
//!
//! 1. по расписанию (через 30 с после старта, далее раз в 4 часа) сходить за
//!    `latest.json` и, если версия новее, **эмитить событие фронту** —
//!    он покажет ненавязчивый баннер «Доступна версия X — [Перезапустить] [Позже]»;
//! 2. по кнопке «Перезапустить» (`updater_install`) — скачать, проверить
//!    minisign-подпись (это делает `tauri-plugin-updater` зашитым pubkey),
//!    запустить NSIS в `passive`-режиме и перезапустить приложение.
//!
//! «Позже» (`updater_snooze`) прячет баннер на 24 часа (состояние — в памяти
//! процесса, при следующем старте баннер покажется снова: 11 §7.4).
//!
//! Модуль кроссплатформенный: на macOS `check()` просто не найдёт подходящей
//! платформы в манифесте и вернёт ошибку/None — фоновой проверке это не мешает,
//! разработка идёт как обычно.
//!
//! ## События во фронт
//!
//! | Событие | Payload | Когда |
//! |---|---|---|
//! | `update:available` | `UpdateInfo` | найдена новая версия — показать баннер |
//! | `update:none` | `()` | проверка прошла, обновлений нет |
//! | `update:progress` | `{ downloaded, total }` | идёт скачивание |
//! | `update:installing` | `()` | скачано, запускается инсталлятор |
//! | `update:error` | `{ message }` | не удалось скачать/установить |

use std::sync::{Mutex, MutexGuard, OnceLock};
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{AppHandle, Emitter};
use tauri_plugin_updater::UpdaterExt;

/// Первая проверка — через 30 с после старта, чтобы не мешать стартовому синку
/// кэша и WS-подключению (04 §6.3).
pub const FIRST_CHECK_DELAY: Duration = Duration::from_secs(30);
/// Дальше — раз в 4 часа.
pub const CHECK_INTERVAL: Duration = Duration::from_secs(4 * 60 * 60);
/// «Позже» — не напоминать сутки.
pub const SNOOZE_DURATION: Duration = Duration::from_secs(24 * 60 * 60);

fn log(msg: &str) {
    eprintln!("[leadchat::updater] {msg}");
}

/// Данные для баннера «Доступна версия X».
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateInfo {
    /// Версия из `latest.json`.
    pub version: String,
    /// Версия, которая запущена сейчас.
    pub current_version: String,
    /// `notes` из манифеста — короткая строка «что нового».
    pub notes: String,
}

/// Текущее состояние для UI (кнопка «Проверить обновления» в «О приложении»).
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdaterStatus {
    /// Идёт установка — второй клик игнорируем.
    pub installing: bool,
    /// Баннер отложен («Позже»).
    pub snoozed: bool,
    /// Секунд до конца отсрочки.
    pub snoozed_for_secs: u64,
    /// Версия, о которой уже сообщили фронту.
    pub offered_version: Option<String>,
}

#[derive(Debug, Default)]
struct UpdaterState {
    snoozed_until: Option<Instant>,
    installing: bool,
    offered_version: Option<String>,
}

static UPDATER_STATE: OnceLock<Mutex<UpdaterState>> = OnceLock::new();

fn state() -> &'static Mutex<UpdaterState> {
    UPDATER_STATE.get_or_init(|| Mutex::new(UpdaterState::default()))
}

fn lock_state() -> MutexGuard<'static, UpdaterState> {
    state()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// Почему проверку можно пропустить. `None` — проверяем.
fn skip_reason(st: &UpdaterState, now: Instant, forced: bool) -> Option<&'static str> {
    if st.installing {
        return Some("установка уже идёт");
    }
    if forced {
        // Ручная проверка из «О приложении» игнорирует отсрочку.
        return None;
    }
    match st.snoozed_until {
        Some(until) if until > now => Some("баннер отложен кнопкой «Позже»"),
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// Расписание
// ---------------------------------------------------------------------------

/// Запустить фоновое расписание проверок. Вызывается из `lib.rs::setup()`.
///
/// Обычный поток вместо async-таймера: работы — один HTTP-запрос раз в 4 часа,
/// тащить ради этого таймер в рантайм Tauri незачем; сама проверка выполняется
/// в рантайме через `block_on`.
pub fn init(app: &AppHandle) -> Result<(), String> {
    let handle = app.clone();
    std::thread::Builder::new()
        .name("leadchat-updater".into())
        .spawn(move || {
            std::thread::sleep(FIRST_CHECK_DELAY);
            loop {
                // Ошибки фоновой проверки не показываем: нет сети — попробуем позже.
                let _ = tauri::async_runtime::block_on(check_and_notify(&handle, false));
                std::thread::sleep(CHECK_INTERVAL);
            }
        })
        .map_err(|e| format!("не удалось запустить поток обновлений: {e}"))?;
    log(&format!(
        "расписание запущено: первая проверка через {} с, далее каждые {} ч",
        FIRST_CHECK_DELAY.as_secs(),
        CHECK_INTERVAL.as_secs() / 3600
    ));
    Ok(())
}

/// Сходить за манифестом и, если есть новая версия, сообщить фронту.
/// **Ничего не скачивает и не ставит** — только уведомляет.
async fn check_and_notify(app: &AppHandle, forced: bool) -> Result<Option<UpdateInfo>, String> {
    // Гард держим только внутри let: MutexGuard не должен жить через await.
    let skip = skip_reason(&lock_state(), Instant::now(), forced);
    if let Some(reason) = skip {
        log(&format!("проверка пропущена: {reason}"));
        return Ok(None);
    }

    let updater = match app.updater() {
        Ok(updater) => updater,
        Err(err) => {
            let message = format!("плагин обновлений недоступен: {err}");
            return finish_check(app, forced, Err(message));
        }
    };

    match updater.check().await {
        Ok(Some(update)) => {
            let info = UpdateInfo {
                version: update.version.clone(),
                current_version: update.current_version.clone(),
                notes: update.body.clone().unwrap_or_default(),
            };
            {
                let mut st = lock_state();
                st.offered_version = Some(info.version.clone());
            }
            log(&format!(
                "доступна версия {} (текущая {})",
                info.version, info.current_version
            ));
            // Дальше решает человек: баннер с «Перезапустить»/«Позже» (11 §7.4).
            let _ = app.emit("update:available", &info);
            Ok(Some(info))
        }
        Ok(None) => {
            log("обновлений нет");
            let _ = app.emit("update:none", ());
            Ok(None)
        }
        Err(err) => finish_check(app, forced, Err(format!("проверка обновления: {err}"))),
    }
}

/// Ошибку ручной проверки показываем пользователю, фоновой — только в лог
/// (04 §6.3: «нет сети/сервера — молча, попробуем в следующий раз»).
fn finish_check(
    app: &AppHandle,
    forced: bool,
    result: Result<Option<UpdateInfo>, String>,
) -> Result<Option<UpdateInfo>, String> {
    match result {
        Ok(value) => Ok(value),
        Err(message) => {
            log(&message);
            if forced {
                let _ = app.emit("update:error", serde_json::json!({ "message": message }));
                Err(message)
            } else {
                Ok(None)
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Команды Tauri
// ---------------------------------------------------------------------------

/// Проверить обновления сейчас (кнопка «Проверить» в «О приложении» и стартовое
/// расписание). Отсрочку «Позже» ручная проверка игнорирует.
#[tauri::command]
pub async fn updater_check(app: AppHandle) -> Result<Option<UpdateInfo>, String> {
    check_and_notify(&app, true).await
}

/// «Перезапустить» в баннере: скачать → проверить подпись → поставить → перезапустить.
///
/// Подпись minisign проверяет сам `tauri-plugin-updater` зашитым в бандл
/// публичным ключом (04 §6.2): подменённый на сервере `signature` или бинарник
/// приводит к ошибке установки, а не к запуску чужого exe.
#[tauri::command]
pub async fn updater_install(app: AppHandle) -> Result<(), String> {
    {
        let mut st = lock_state();
        if st.installing {
            return Err("Установка уже идёт".into());
        }
        st.installing = true;
    }

    let result = run_install(&app).await;

    if let Err(err) = &result {
        lock_state().installing = false;
        log(&format!("установка не удалась: {err}"));
        let _ = app.emit("update:error", serde_json::json!({ "message": err }));
    }
    result
}

// `app.restart()` в Tauri 2 не возвращает управление; `Ok(())` ниже оставлен на
// случай, если инсталлятор уже закрыл приложение сам.
#[allow(unreachable_code)]
async fn run_install(app: &AppHandle) -> Result<(), String> {
    let updater = app
        .updater()
        .map_err(|e| format!("плагин обновлений недоступен: {e}"))?;

    // Перепроверяем перед установкой: между показом баннера и кликом могли
    // выложить более свежую версию (или откатить релиз).
    let update = updater
        .check()
        .await
        .map_err(|e| format!("проверка обновления: {e}"))?
        .ok_or_else(|| "Обновление больше не актуально".to_string())?;

    let version = update.version.clone();
    log(&format!("скачиваем версию {version}"));

    let progress_app = app.clone();
    let finish_app = app.clone();
    let mut downloaded: u64 = 0;

    update
        .download_and_install(
            move |chunk_length: usize, content_length: Option<u64>| {
                downloaded = downloaded.saturating_add(chunk_length as u64);
                let _ = progress_app.emit(
                    "update:progress",
                    serde_json::json!({ "downloaded": downloaded, "total": content_length }),
                );
            },
            move || {
                let _ = finish_app.emit("update:installing", ());
            },
        )
        .await
        .map_err(|e| format!("установка обновления: {e}"))?;

    log(&format!("версия {version} установлена, перезапускаемся"));
    // NSIS в passive-режиме закрывает приложение сам; вызов ниже — на случай,
    // если инсталлятор вернул управление (04 §6.3, CHECK для Windows).
    // Очередь outbox переживает перезапуск: она в SQLite, а не в памяти.
    app.restart();
    Ok(())
}

/// «Позже» в баннере: не напоминать 24 часа (до перезапуска приложения).
#[tauri::command]
pub fn updater_snooze(app: AppHandle) -> Result<(), String> {
    {
        let mut st = lock_state();
        st.snoozed_until = Some(Instant::now() + SNOOZE_DURATION);
    }
    log("баннер отложен на 24 часа");
    let _ = app.emit(
        "update:snoozed",
        serde_json::json!({ "hours": SNOOZE_DURATION.as_secs() / 3600 }),
    );
    Ok(())
}

/// Состояние апдейтера для экрана «О приложении».
#[tauri::command]
pub fn updater_status() -> Result<UpdaterStatus, String> {
    let st = lock_state();
    let now = Instant::now();
    let snoozed_for = st
        .snoozed_until
        .filter(|until| *until > now)
        .map(|until| until.saturating_duration_since(now).as_secs())
        .unwrap_or(0);
    Ok(UpdaterStatus {
        installing: st.installing,
        snoozed: snoozed_for > 0,
        snoozed_for_secs: snoozed_for,
        offered_version: st.offered_version.clone(),
    })
}

// ---------------------------------------------------------------------------
// Тесты
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fresh_state_allows_check() {
        let st = UpdaterState::default();
        assert_eq!(skip_reason(&st, Instant::now(), false), None);
    }

    #[test]
    fn snooze_blocks_background_checks_but_not_manual() {
        let now = Instant::now();
        let st = UpdaterState {
            snoozed_until: Some(now + SNOOZE_DURATION),
            ..Default::default()
        };
        assert!(
            skip_reason(&st, now, false).is_some(),
            "фоновая — пропускаем"
        );
        assert_eq!(
            skip_reason(&st, now, true),
            None,
            "ручная — проверяем всегда"
        );
    }

    #[test]
    fn snooze_expires_after_24h() {
        let now = Instant::now();
        let st = UpdaterState {
            snoozed_until: Some(now + Duration::from_secs(1)),
            ..Default::default()
        };
        let later = now + SNOOZE_DURATION + Duration::from_secs(60);
        assert_eq!(skip_reason(&st, later, false), None);
    }

    #[test]
    fn install_in_progress_blocks_everything() {
        let st = UpdaterState {
            installing: true,
            ..Default::default()
        };
        assert!(skip_reason(&st, Instant::now(), true).is_some());
    }

    #[test]
    fn schedule_matches_spec() {
        assert_eq!(FIRST_CHECK_DELAY.as_secs(), 30);
        assert_eq!(CHECK_INTERVAL.as_secs(), 4 * 3600);
        assert_eq!(SNOOZE_DURATION.as_secs(), 24 * 3600);
    }
}
