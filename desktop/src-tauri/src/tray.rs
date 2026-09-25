//! Трей: иконка, меню, бейдж непрочитанных (04-DESKTOP §3).
//!
//! ```text
//! ┌──────────────────────────────┐
//! │ ● На месте                   │  ← ровно один активен
//! │ ○ Отошёл                     │
//! ├──────────────────────────────┤
//! │ Новые чаты: 3                │  ← disabled, текст обновляет set_badge
//! ├──────────────────────────────┤
//! │ Развернуть LeadChat          │  ← то же, что ЛКМ по иконке / Ctrl+Shift+L
//! ├──────────────────────────────┤
//! │ Выход                        │  ← единственный способ завершить процесс
//! └──────────────────────────────┘
//! ```
//!
//! Иконки — заранее отрендеренные PNG (`icons/tray/*.png`, генератор
//! `scripts/gen-tray-icons.mjs`): рантайм только выбирает файл, никакой
//! растеризации шрифтов. Файлы вшиты в бинарник `include_bytes!` — не зависим
//! от раскладки ресурсов и работаем одинаково в dev и в установленном виде.

use std::sync::Mutex;

use tauri::image::Image;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, Wry};

use crate::{EVENT_PRESENCE_SET, MAIN_WINDOW};

/// Идентификатор трея из `app.trayIcon.id` в tauri.conf.json.
pub const TRAY_ID: &str = "main";

/// Сколько ждём отправку очереди перед выходом из приложения (04 §3.3).
const EXIT_FLUSH_TIMEOUT_SECS: u64 = 3;

const ID_HERE: &str = "st_here";
const ID_AWAY: &str = "st_away";
const ID_UNREAD: &str = "unread";
const ID_OPEN: &str = "open";
const ID_QUIT: &str = "quit";

/// Статус сотрудника (01-API-SPEC §11.6: `online` | `away`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Presence {
    #[default]
    Online,
    Away,
}

impl Presence {
    pub fn as_str(self) -> &'static str {
        match self {
            Presence::Online => "online",
            Presence::Away => "away",
        }
    }

    pub fn parse(raw: &str) -> Option<Self> {
        match raw {
            "online" => Some(Presence::Online),
            "away" => Some(Presence::Away),
            _ => None,
        }
    }
}

/// Текущая модель трея: то, что нарисовано в меню и на иконке.
#[derive(Debug, Default)]
struct TrayModel {
    unread: u32,
    presence: Presence,
}

/// Хендлы пунктов меню — чтобы обновлять текст и галки без пересборки меню.
pub struct TrayState {
    unread: MenuItem<Wry>,
    here: CheckMenuItem<Wry>,
    away: CheckMenuItem<Wry>,
    model: Mutex<TrayModel>,
}

// --- иконки: вшиты в бинарник (32×32 трей, 16×16 оверлей таскбара) ----------

const TRAY_ICONS: [&[u8]; 10] = [
    include_bytes!("../icons/tray/tray_0.png"),
    include_bytes!("../icons/tray/tray_1.png"),
    include_bytes!("../icons/tray/tray_2.png"),
    include_bytes!("../icons/tray/tray_3.png"),
    include_bytes!("../icons/tray/tray_4.png"),
    include_bytes!("../icons/tray/tray_5.png"),
    include_bytes!("../icons/tray/tray_6.png"),
    include_bytes!("../icons/tray/tray_7.png"),
    include_bytes!("../icons/tray/tray_8.png"),
    include_bytes!("../icons/tray/tray_9.png"),
];
const TRAY_ICON_9PLUS: &[u8] = include_bytes!("../icons/tray/tray_9plus.png");

#[cfg(target_os = "windows")]
const BADGE_ICONS: [&[u8]; 9] = [
    include_bytes!("../icons/tray/badge_1.png"),
    include_bytes!("../icons/tray/badge_2.png"),
    include_bytes!("../icons/tray/badge_3.png"),
    include_bytes!("../icons/tray/badge_4.png"),
    include_bytes!("../icons/tray/badge_5.png"),
    include_bytes!("../icons/tray/badge_6.png"),
    include_bytes!("../icons/tray/badge_7.png"),
    include_bytes!("../icons/tray/badge_8.png"),
    include_bytes!("../icons/tray/badge_9.png"),
];
#[cfg(target_os = "windows")]
const BADGE_ICON_9PLUS: &[u8] = include_bytes!("../icons/tray/badge_9plus.png");

fn tray_icon_bytes(count: u32) -> &'static [u8] {
    match count {
        0..=9 => TRAY_ICONS[count as usize],
        _ => TRAY_ICON_9PLUS,
    }
}

// ---------------------------------------------------------------------------

/// Собрать меню трея и навесить обработчики (вызывается из `setup`).
pub fn init(app: &AppHandle) -> tauri::Result<()> {
    let here = CheckMenuItem::with_id(app, ID_HERE, "На месте", true, true, None::<&str>)?;
    let away = CheckMenuItem::with_id(app, ID_AWAY, "Отошёл", true, false, None::<&str>)?;
    let unread = MenuItem::with_id(app, ID_UNREAD, unread_label(0), false, None::<&str>)?;
    let open = MenuItem::with_id(app, ID_OPEN, "Развернуть LeadChat", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, ID_QUIT, "Выход", true, None::<&str>)?;

    let menu = Menu::with_items(
        app,
        &[
            &here,
            &away,
            &PredefinedMenuItem::separator(app)?,
            &unread,
            &PredefinedMenuItem::separator(app)?,
            &open,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )?;

    let tray = app
        .tray_by_id(TRAY_ID)
        .expect("трей объявлен в tauri.conf.json (app.trayIcon.id = \"main\")");
    tray.set_menu(Some(menu))?;
    // ЛКМ — развернуть окно, ПКМ — меню (04 §3.1).
    tray.set_show_menu_on_left_click(false)?;

    tray.on_tray_icon_event(|tray, event| {
        if let TrayIconEvent::Click {
            button: MouseButton::Left,
            button_state: MouseButtonState::Up,
            ..
        } = event
        {
            show_main(tray.app_handle());
        }
    });

    tray.on_menu_event(move |app, event| match event.id.as_ref() {
        ID_OPEN => show_main(app),
        // Единственный способ действительно завершить процесс (04 §3.3).
        // Перед выходом — best-effort прогон очереди с таймаутом 3 с: то, что
        // оператор успел набрать, не должно осесть в кэше до следующего запуска.
        ID_QUIT => {
            crate::sync::flush_before_exit(app, EXIT_FLUSH_TIMEOUT_SECS);
            app.exit(0)
        }
        // Статус меняет фронт: он делает PUT /api/v1/presence (01 §11.6) и возвращает
        // подтверждённое значение командой set_presence. Rust сам в API не ходит.
        ID_HERE => request_presence(app, Presence::Online),
        ID_AWAY => request_presence(app, Presence::Away),
        _ => {}
    });

    app.manage(TrayState {
        unread,
        here,
        away,
        model: Mutex::new(TrayModel::default()),
    });

    // Привести иконку и тултип в согласованное состояние на старте.
    set_badge(app, 0)?;
    Ok(())
}

/// Бейдж непрочитанных: иконка трея + тултип + пункт меню + оверлей таскбара (04 §3.2).
pub fn set_badge(app: &AppHandle, count: u32) -> tauri::Result<()> {
    let icon = Image::from_bytes(tray_icon_bytes(count))?;
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        tray.set_icon(Some(icon))?;
    }

    if let Some(state) = app.try_state::<TrayState>() {
        state.unread.set_text(unread_label(count))?;
        if let Ok(mut model) = state.model.lock() {
            model.unread = count;
        }
    }
    apply_tooltip(app)?;
    set_taskbar_overlay(app, count);
    Ok(())
}

/// Синхронизировать галки статуса, когда статус сменили из UI (04 §3.1).
pub fn set_presence(app: &AppHandle, status: &str) -> Result<(), String> {
    let presence =
        Presence::parse(status).ok_or_else(|| format!("неизвестный статус: {status}"))?;
    apply_presence(app, presence).map_err(|e| e.to_string())
}

fn apply_presence(app: &AppHandle, presence: Presence) -> tauri::Result<()> {
    if let Some(state) = app.try_state::<TrayState>() {
        state.here.set_checked(presence == Presence::Online)?;
        state.away.set_checked(presence == Presence::Away)?;
        if let Ok(mut model) = state.model.lock() {
            model.presence = presence;
        }
    }
    apply_tooltip(app)
}

/// Клик по пункту статуса: оптимистично переставляем галку и просим фронт сходить в API.
fn request_presence(app: &AppHandle, presence: Presence) {
    if let Err(err) = apply_presence(app, presence) {
        log::warn!("трей: не удалось обновить статус: {err}");
    }
    let _ = app.emit(EVENT_PRESENCE_SET, presence.as_str());
}

fn unread_label(count: u32) -> String {
    if count == 0 {
        "Новых чатов нет".to_string()
    } else {
        format!("Новые чаты: {count}")
    }
}

fn apply_tooltip(app: &AppHandle) -> tauri::Result<()> {
    let (unread, presence) = match app.try_state::<TrayState>() {
        Some(state) => match state.model.lock() {
            Ok(model) => (model.unread, model.presence),
            Err(_) => (0, Presence::Online),
        },
        None => (0, Presence::Online),
    };

    let mut tooltip = String::from("LeadChat");
    if unread > 0 {
        tooltip.push_str(&format!(" — новых: {unread}"));
    }
    if presence == Presence::Away {
        tooltip.push_str(" — отошёл");
    }

    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        tray.set_tooltip(Some(tooltip))?;
    }
    Ok(())
}

/// Оверлей на кнопке таскбара — Windows-only API (04 §3.2).
#[cfg(target_os = "windows")]
fn set_taskbar_overlay(app: &AppHandle, count: u32) {
    let Some(window) = app.get_webview_window(MAIN_WINDOW) else {
        return;
    };
    if count == 0 {
        let _ = window.set_overlay_icon(None);
        return;
    }
    let bytes = match count {
        1..=9 => BADGE_ICONS[(count - 1) as usize],
        _ => BADGE_ICON_9PLUS,
    };
    match Image::from_bytes(bytes) {
        Ok(icon) => {
            let _ = window.set_overlay_icon(Some(icon));
        }
        Err(err) => log::warn!("оверлей таскбара: не удалось прочитать иконку: {err}"),
    }
}

/// На macOS/Linux оверлея таскбара нет — заглушка, чтобы `cargo check` проходил везде.
#[cfg(not(target_os = "windows"))]
fn set_taskbar_overlay(_app: &AppHandle, _count: u32) {}

// --- окно ------------------------------------------------------------------

/// Развернуть и сфокусировать главное окно.
pub fn show_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

/// Показать/спрятать окно — ЛКМ по трею, пункт меню, Ctrl+Shift+L.
pub fn toggle_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let visible = window.is_visible().unwrap_or(false);
        let focused = window.is_focused().unwrap_or(false);
        if visible && focused {
            let _ = window.hide();
        } else {
            show_main(app);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn icon_matches_count() {
        assert_eq!(tray_icon_bytes(0), TRAY_ICONS[0]);
        assert_eq!(tray_icon_bytes(7), TRAY_ICONS[7]);
        assert_eq!(tray_icon_bytes(9), TRAY_ICONS[9]);
        assert_eq!(tray_icon_bytes(10), TRAY_ICON_9PLUS);
        assert_eq!(tray_icon_bytes(1_000), TRAY_ICON_9PLUS);
    }

    #[test]
    fn unread_label_is_human() {
        assert_eq!(unread_label(0), "Новых чатов нет");
        assert_eq!(unread_label(3), "Новые чаты: 3");
    }

    #[test]
    fn presence_roundtrip() {
        assert_eq!(Presence::parse("away"), Some(Presence::Away));
        assert_eq!(Presence::parse("online"), Some(Presence::Online));
        assert_eq!(Presence::parse("busy"), None);
        assert_eq!(Presence::Away.as_str(), "away");
    }
}
