# 04 — Десктоп-приложение Windows (Tauri 2)

> Детализация разделов 1.7, 3.6 и этапа 4 плана из [DESIGN.md](../DESIGN.md). Решения оттуда не пересматриваются: Tauri 2, тот же React-бандл, что и веб, данные через тот же REST + WebSocket на `chat.partner-lead-centre.ru/api`, локальный SQLite-кэш, шифрование DPAPI, автообновление с подписью.
>
> Код в документе — «близко к рабочему»: API Tauri 2 и crate `windows` уточнить по версиям на момент старта (Tauri 2.x, tauri-plugin-\* 2.x). Места, где нужна сверка, помечены `// CHECK`.

---

## 1. Структура проекта и конфигурация

### 1.1. Расположение в репозитории

Десктоп — это **обёртка над тем же собранным React-бандлом**, что и веб. Никакого второго фронтенда:

```
leadchat/
├── frontend/                      # общий React 18 + TS + Vite (см. 03-FRONTEND)
│   ├── src/
│   │   ├── platform/              # единственное «десктоп-осознанное» место фронта (03 §1.1/§7)
│   │   │   ├── bridge.ts          # интерфейс PlatformBridge + isTauri() + getBridge()
│   │   │   ├── web.ts             # веб-заглушка
│   │   │   └── tauri/             # реализация моста (ленивые импорты @tauri-apps/*)
│   │   │       ├── index.ts       # createTauriBridge()
│   │   │       ├── notifier.ts    # решение «показывать ли тост» + троттлинг (разд. 4)
│   │   │       ├── offline.ts     # чтение кэша, outbox, синхронизация (разд. 5)
│   │   │       └── updater.ts     # проверка обновлений, баннер (разд. 6)
│   │   └── ...
│   └── dist/                      # прод-бандл; его же грузит Tauri
│
├── desktop/
│   ├── package.json               # только @tauri-apps/cli + плагины JS-биндингов
│   └── src-tauri/
│       ├── Cargo.toml
│       ├── tauri.conf.json        # см. 1.3
│       ├── capabilities/main.json # permissions Tauri 2 — см. 8.3
│       ├── icons/                 # icon.ico, 32x32.png, 128x128.png …
│       │   └── tray/              # tray_0.png … tray_9.png, tray_9plus.png,
│       │                          # badge_1.png … badge_9plus.png (overlay для таскбара)
│       ├── migrations/            # SQL-миграции локального кэша (0001_init.sql, …)
│       └── src/
│           ├── main.rs            # только вызов leadchat_desktop::run()
│           ├── lib.rs             # Builder: плагины, setup, on_window_event
│           ├── tray.rs            # трей: меню, иконка, бейдж (разд. 3)
│           ├── notify.rs          # WinRT-тосты с кнопкой «Ответить» (разд. 4)
│           ├── deeplink.rs        # leadchat:// → событие navigate во фронт
│           ├── commands.rs        # все #[tauri::command] (разд. 8)
│           └── cache/
│               ├── mod.rs         # инициализация: ключ DPAPI, открытие БД
│               ├── dpapi.rs       # CryptProtectData/CryptUnprotectData (разд. 5.5)
│               ├── crypto.rs      # AES-256-GCM поверх мастер-ключа
│               ├── store.rs       # dialogs / cached_messages: upsert, чтение, prune
│               └── outbox.rs      # исходящая очередь + flush (разд. 5.3)
│
└── .github/workflows/desktop-release.yml   # разд. 7
```

Сборка фронта для десктопа — тот же `vite build`, но с `--mode desktop`: единственное отличие — `VITE_API_BASE=https://chat.partner-lead-centre.ru` (в вебе API относительный, в Tauri origin — `tauri://localhost`, поэтому база абсолютная) и флаг `VITE_IS_DESKTOP=1` для tree-shaking десктоп-модулей из веб-бандла.

### 1.2. Cargo.toml (зависимости)

```toml
[package]
name = "leadchat-desktop"
version = "1.0.0"           # источник истины версии — tauri.conf.json
edition = "2021"

[lib]
name = "leadchat_desktop"
crate-type = ["staticlib", "cdylib", "rlib"]

[build-dependencies]
tauri-build = { version = "2", features = [] }

[dependencies]
tauri = { version = "2", features = ["tray-icon", "image-png"] }
tauri-plugin-single-instance = { version = "2", features = ["deep-link"] }
tauri-plugin-deep-link = "2"
tauri-plugin-notification = "2"
tauri-plugin-autostart = "2"
tauri-plugin-global-shortcut = "2"
tauri-plugin-sql = { version = "2", features = ["sqlite"] }
tauri-plugin-updater = "2"
tauri-plugin-process = "2"

serde = { version = "1", features = ["derive"] }
serde_json = "1"
uuid = { version = "1", features = ["v4"] }
aes-gcm = "0.10"            # шифрование колонок кэша
rand = "0.8"
rusqlite = { version = "0.31", features = ["bundled"] }  # прямой доступ к кэшу из Rust-команд
reqwest = { version = "0.12", features = ["json", "rustls-tls"] }  # outbox_flush

[target.'cfg(windows)'.dependencies]
windows = { version = "0.58", features = [
  "Win32_Security_Cryptography",   # DPAPI
  "Win32_Foundation",
  "Win32_System_Memory",
  "Data_Xml_Dom",                  # WinRT toast XML
  "UI_Notifications",              # ToastNotificationManager
] }
```

### 1.3. Полный `tauri.conf.json`

```json
{
  "$schema": "https://schema.tauri.app/config/2",
  "productName": "LeadChat",
  "version": "1.0.0",
  "identifier": "ru.partner-lead-centre.leadchat",

  "build": {
    "beforeDevCommand": "npm --prefix ../../frontend run dev",
    "devUrl": "http://localhost:5173",
    "beforeBuildCommand": "npm --prefix ../../frontend run build -- --mode desktop",
    "frontendDist": "../../frontend/dist"
  },

  "app": {
    "windows": [
      {
        "label": "main",
        "title": "LeadChat by Lead Partner",
        "width": 1280,
        "height": 800,
        "minWidth": 1024,
        "minHeight": 640,
        "center": true,
        "resizable": true,
        "visible": true,
        "dragDropEnabled": true
      }
    ],
    "security": {
      "csp": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src ipc: http://ipc.localhost https://chat.partner-lead-centre.ru wss://chat.partner-lead-centre.ru; img-src 'self' data: https://chat.partner-lead-centre.ru; font-src 'self'"
    },
    "trayIcon": {
      "id": "main",
      "iconPath": "icons/tray/tray_0.png",
      "tooltip": "LeadChat"
    }
  },

  "bundle": {
    "active": true,
    "targets": ["nsis", "msi"],
    "icon": ["icons/32x32.png", "icons/128x128.png", "icons/icon.ico"],
    "createUpdaterArtifacts": true,
    "windows": {
      "webviewInstallMode": { "type": "downloadBootstrapper" },
      "nsis": {
        "installMode": "currentUser",
        "languages": ["Russian"],
        "displayLanguageSelector": false,
        "startMenuFolder": "Lead Partner"
      },
      "wix": {
        "language": "ru-RU"
      }
    }
  },

  "plugins": {
    "updater": {
      "endpoints": [
        "https://chat.partner-lead-centre.ru/download/latest.json"
      ],
      "pubkey": "<MINISIGN_PUBLIC_KEY — из tauri signer generate, см. разд. 6.2>",
      "windows": { "installMode": "passive" }
    },
    "deep-link": {
      "desktop": { "schemes": ["leadchat"] }
    }
  }
}
```

Пояснения к неочевидным местам:

- **`targets: ["nsis", "msi"]`** — оба инсталлятора из DESIGN.md, но роли разные. **NSIS с `installMode: "currentUser"` — основной для сотрудников**: ставится в `%LOCALAPPDATA%`, без прав администратора и UAC («скачал → два клика → работает»), и именно он участвует в автообновлении. **MSI (WiX)** — для централизованного развёртывания через GPO/Intune, если ИТ захочет ставить всем разом; WiX в Tauri ставит per-machine, обновляется тоже централизованно, автообновление в нём выключаем флагом при установке (`MSIINSTALLPERUSER` у Tauri-WiX нет — это ограничение WiX-шаблона, а не наше решение).
- **`createUpdaterArtifacts: true`** — рядом с `*-setup.exe` появится `*.sig` (minisign-подпись) — это и есть артефакты updater'а в Tauri 2.
- **`webviewInstallMode: downloadBootstrapper`** — на Windows 10/11 WebView2 уже есть; бутстраппер сработает только на голых машинах, инсталлятор остаётся ~5–8 МБ.
- **CSP**: фронт общается только со своим доменом; `ipc:`/`http://ipc.localhost` — служебные origin'ы IPC Tauri. Никаких CDN — весь UI в бандле.
- **`app.trayIcon`** объявляет трей декларативно; меню и обработчики навешиваются в Rust (`tray.rs`) через `app.tray_by_id("main")`.
- **Close-to-tray настраивается не в конфиге**, а в коде (`on_window_event`, см. 3.3) — в Tauri 2 такой опции в `tauri.conf.json` нет.

### 1.4. Скелет `lib.rs` (порядок инициализации важен)

```rust
use tauri::{Emitter, Manager};
use tauri_plugin_autostart::MacosLauncher;

pub fn run() {
    tauri::Builder::default()
        // 1. single-instance — строго ПЕРВЫМ (иначе вторая копия успеет
        //    инициализировать плагины до проверки)
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            crate::tray::show_main(app);
            // deep-link второй копии приходит аргументом командной строки
            if let Some(url) = argv.iter().find(|a| a.starts_with("leadchat://")) {
                let _ = app.emit("navigate", crate::deeplink::parse(url));
            }
        }))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,             // на Windows игнорируется
            Some(vec!["--minimized"]),              // автозапуск → сразу в трей
        ))
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(
            tauri_plugin_sql::Builder::default()
                .add_migrations("sqlite:cache.db", crate::cache::migrations())
                .build(),
        )
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .setup(|app| {
            crate::cache::init(app.handle())?;      // DPAPI-ключ + открытие БД (разд. 5.5)
            crate::tray::init(app.handle())?;       // меню трея (разд. 3.1)
            crate::notify::init(app.handle())?;     // AUMID + COM-активатор тостов (разд. 4.2)
            crate::deeplink::init(app.handle())?;   // on_open_url → событие navigate

            // Ctrl+Shift+L — развернуть/спрятать (DESIGN 3.6)
            use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};
            let handle = app.handle().clone();
            app.global_shortcut().on_shortcut("ctrl+shift+l", move |_app, _sc, ev| {
                if ev.state() == ShortcutState::Pressed {
                    crate::tray::toggle_main(&handle);
                }
            })?;

            // запуск из автостарта — не показывать окно
            if std::env::args().any(|a| a == "--minimized") {
                if let Some(w) = app.get_webview_window("main") { let _ = w.hide(); }
            }
            Ok(())
        })
        // закрытие окна = свернуть в трей (разд. 3.3)
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .invoke_handler(tauri::generate_handler![
            crate::commands::set_badge,
            crate::commands::notify_reply,
            crate::commands::outbox_push,
            crate::commands::outbox_flush,
            crate::commands::dpapi_encrypt,
            crate::commands::dpapi_decrypt,
            crate::commands::cache_apply_sync,
            crate::commands::cache_load_dialogs,
            crate::commands::cache_load_messages,
            crate::commands::set_presence,
            crate::commands::set_session_token,
        ])
        .run(tauri::generate_context!())
        .expect("LeadChat: failed to start");
}
```

Во фронте единая точка проверки:

```ts
// frontend/src/platform/bridge.ts — полный код и интерфейс PlatformBridge: 03 §7
export function isTauri(): boolean {
  return "__TAURI_INTERNALS__" in window;   // инжектится Tauri 2 в WebView
}
// Tauri-реализация моста (platform/tauri/) импортируется лениво в initBridge():
// в веб-бандле десктоп-модулей нет.
```

---

## 2. Плагины Tauri 2 и их роль в LeadChat

| Плагин / фича | Crate | Что делает именно в LeadChat |
|---|---|---|
| **tray-icon** | фича `tray-icon` ядра `tauri` (не отдельный плагин) | Иконка в трее с меню (статус, счётчик, развернуть, выход) и бейджем непрочитанных; точка возврата после close-to-tray. Разд. 3 |
| **notification** | `tauri-plugin-notification` | Простые тосты: «аккаунт Авито требует переподключения» (`needs_reauth` из DESIGN 1.5), «обновление установлено», сводные уведомления. Интерактивный тост с кнопкой «Ответить» делает не плагин, а наш `notify.rs` через WinRT (разд. 4.2) — у плагина на Windows нет кнопок и поля ввода |
| **autostart** | `tauri-plugin-autostart` | Галка «Запускать при старте Windows» в `/settings/profile` (ключ реестра `HKCU\...\Run`). Запускаем с аргументом `--minimized` — приложение стартует сразу в трей |
| **global-shortcut** | `tauri-plugin-global-shortcut` | Единственный глобальный хоткей `Ctrl+Shift+L` — развернуть/свернуть LeadChat из любого приложения (DESIGN 3.6) |
| **sql (SQLite)** | `tauri-plugin-sql` (sqlx/sqlite) | Файл `cache.db` в `%APPDATA%\ru.partner-lead-centre.leadchat\`: кэш 200 диалогов, сообщения, outbox. Плагин отвечает за файл и миграции; **чтение/запись зашифрованных колонок — только через Rust-команды** (разд. 5), JS-API плагина фронт использует лишь для нечувствительных полей (счётчики, `meta`) |
| **updater** | `tauri-plugin-updater` | Автообновление: `latest.json` с нашего домена, проверка minisign-подписи, тихая установка NSIS `passive` (разд. 6) |
| **single-instance** | `tauri-plugin-single-instance` | Вторая копия не запускается: существующее окно разворачивается, аргументы (в т.ч. deep-link) пробрасываются в первую копию. Регистрируется первым плагином |
| **deep-link** | `tauri-plugin-deep-link` | Схема `leadchat://` в реестре Windows. `leadchat://chats/{conversation_id}` — открыть конкретный диалог: используется тостами (клик по уведомлению), а в будущем — ссылками «Открыть в приложении» из веб-версии |
| **process** (служебный) | `tauri-plugin-process` | `relaunch()` после установки обновления |

Ограничение поверхности: каждый плагин получает только явно перечисленные permissions в `capabilities/main.json` (разд. 8.3) — например, у `sql` нет `allow-load` произвольных БД, у `global-shortcut` нет регистрации шорткатов из JS.

---

## 3. Трей

### 3.1. Меню и поведение

```
┌──────────────────────────────┐
│ ● На месте                   │  ← CheckMenuItem-группа: ровно один активен
│ ○ Отошёл                     │
├──────────────────────────────┤
│ Новые чаты: 3                │  ← disabled-пункт, текст обновляется
├──────────────────────────────┤
│ Развернуть LeadChat          │  ← то же, что ЛКМ по иконке / Ctrl+Shift+L
├──────────────────────────────┤
│ Выход                        │  ← единственный способ реально закрыть процесс
└──────────────────────────────┘
```

```rust
// src-tauri/src/tray.rs
use tauri::{
    menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconEvent},
    AppHandle, Emitter, Manager,
};

pub fn init(app: &AppHandle) -> tauri::Result<()> {
    let st_here = CheckMenuItem::with_id(app, "st_here", "На месте", true, true, None::<&str>)?;
    let st_away = CheckMenuItem::with_id(app, "st_away", "Отошёл",  true, false, None::<&str>)?;
    let unread  = MenuItem::with_id(app, "unread", "Новых чатов нет", false, None::<&str>)?;
    let open    = MenuItem::with_id(app, "open", "Развернуть LeadChat", true, None::<&str>)?;
    let quit    = MenuItem::with_id(app, "quit", "Выход", true, None::<&str>)?;
    let sep     = PredefinedMenuItem::separator(app)?;

    let menu = Menu::with_items(app, &[
        &st_here, &st_away, &sep, &unread,
        &PredefinedMenuItem::separator(app)?, &open,
        &PredefinedMenuItem::separator(app)?, &quit,
    ])?;

    let tray = app.tray_by_id("main").expect("tray declared in tauri.conf.json");
    tray.set_menu(Some(menu))?;
    tray.set_show_menu_on_left_click(false)?;   // ЛКМ — развернуть, ПКМ — меню

    tray.on_tray_icon_event(|tray, event| {
        if let TrayIconEvent::Click { button: MouseButton::Left,
                                      button_state: MouseButtonState::Up, .. } = event {
            show_main(tray.app_handle());
        }
    });

    let handle = app.clone();
    tray.on_menu_event(move |app, event| match event.id.as_ref() {
        "open" => show_main(app),
        "quit" => app.exit(0),                  // обходит close-to-tray
        // Статус шлём во фронт: он делает PUT /api/v1/presence
        // (01-API-SPEC §11.6, значения online|away) и по ответу переключает галки
        "st_here" => { let _ = handle.emit("presence:set", "online"); }
        "st_away" => { let _ = handle.emit("presence:set", "away"); }
        _ => {}
    });
    Ok(())
}

pub fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show(); let _ = w.unminimize(); let _ = w.set_focus();
    }
}

pub fn toggle_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        if w.is_visible().unwrap_or(false) && w.is_focused().unwrap_or(false) {
            let _ = w.hide();
        } else { show_main(app); }
    }
}
```

Синхронизация статуса — двусторонняя: если статус сменили в UI, фронт вызывает команду `set_presence("away")`, Rust переставляет `CheckMenuItem`-галки (`st_here.set_checked(false)` и т.д.) и обновляет tooltip иконки («LeadChat — отошёл»).

### 3.2. Бейдж непрочитанных

Два индикатора, обновляются одной командой `set_badge(count)`:

1. **Иконка трея.** Не рисуем текст в рантайме — в `icons/tray/` лежат **заранее отрендеренные** PNG 32×32: `tray_0.png` (без бейджа), `tray_1.png` … `tray_9.png`, `tray_9plus.png` (красный кружок с цифрой в правом нижнем углу, стиль бренда Lead Partner). Рантайм только выбирает файл — надёжнее и без зависимости на растеризацию шрифтов:

```rust
// commands.rs
#[tauri::command]
pub fn set_badge(app: tauri::AppHandle, count: u32) -> Result<(), String> {
    let name = match count { 0 => "tray_0".into(), 1..=9 => format!("tray_{count}"),
                             _ => "tray_9plus".into() };
    let icon = tauri::image::Image::from_path(
        app.path().resource_dir().map_err(|e| e.to_string())?
            .join(format!("icons/tray/{name}.png"))).map_err(|e| e.to_string())?;

    if let Some(tray) = app.tray_by_id("main") {
        tray.set_icon(Some(icon)).map_err(|e| e.to_string())?;
        tray.set_tooltip(Some(if count == 0 { "LeadChat".into() }
                              else { format!("LeadChat — новых: {count}") })).ok();
    }
    // Оверлей на кнопке таскбара (когда окно развёрнуто) — badge_*.png 16×16
    if let Some(w) = app.get_webview_window("main") {
        if count == 0 { w.set_overlay_icon(None).ok(); }
        else {
            let b = match count { 1..=9 => format!("badge_{count}"), _ => "badge_9plus".into() };
            let img = tauri::image::Image::from_path(
                app.path().resource_dir().unwrap().join(format!("icons/tray/{b}.png"))).ok();
            w.set_overlay_icon(img).ok();   // Windows-only API — то, что нужно
        }
    }
    // пункт меню «Новые чаты: N» обновляется здесь же (unread.set_text(...))
    Ok(())
}
```

2. **Кто считает.** Источник счётчика — фронтенд: он уже держит список диалогов с непрочитанными (TanStack Query + WS-события `message:new`). На каждое изменение суммарного счётчика — debounce 300 мс → `invoke("set_badge", { count })`. В офлайне счётчик берётся из кэша (`SELECT sum(unread) FROM dialogs`).

Файлы `icons/tray/*.png` генерируются один раз скриптом `desktop/scripts/gen-tray-icons.mjs` (sharp: базовая иконка + кружок `#E03131` + цифра, шрифт бренда) и коммитятся в репозиторий.

### 3.3. Поведение при закрытии окна

- Крестик / Alt+F4 → `CloseRequested` перехватывается (`api.prevent_close()`), окно прячется, процесс живёт: WS-соединение, тосты и трей продолжают работать. Это ожидаемое поведение мессенджера (DESIGN 3.6).
- При первом сворачивании в трей — одноразовый тост «LeadChat продолжает работать в трее» (флаг в `meta`-таблице кэша).
- Реальное завершение: пункт «Выход» в трее (`app.exit(0)`) или завершение сеанса Windows. Перед `exit` — best-effort `outbox_flush` с таймаутом 3 с.
- Обновление через updater закрывает приложение само (разд. 6.3) — обработчик `CloseRequested` это не блокирует, т.к. updater вызывает `app.exit`, а не закрытие окна.

---

## 4. Уведомления Windows

### 4.1. Матрица «когда показывать тост»

Решение принимает фронтенд (`platform/tauri/notifier.ts`) — у него уже есть WS-события и знание, какой диалог открыт:

| Ситуация | Действие |
|---|---|
| Окно в фокусе, этот диалог открыт | Тоста нет; звук + счётчик в списке |
| Окно в фокусе, диалог не открыт | Тоста нет; звук + бейдж |
| Окно скрыто/свёрнуто/не в фокусе | Тост с текстом и «Ответить» |
| Статус «отошёл» | Тосты показываются (для того и статус), но без звука |
| Сообщение от бота (`sender_type = bot`) | Тост не показывается — бот и должен работать сам; тост придёт при handoff |
| Событие handoff «диалог передан вам» (⚑) | Тост всегда, даже в фокусе — приоритетное |
| `needs_reauth` аккаунта Авито (DESIGN 1.5) | Тост админам через plugin-notification, клик → `/settings/accounts` |

### 4.2. Интерактивный тост: текст + кнопка «Ответить»

`tauri-plugin-notification` на Windows не умеет кнопки и поле ввода, поэтому богатый тост собирается в `notify.rs` напрямую через WinRT (`windows` crate, `ToastNotificationManager`). XML тоста:

```xml
<toast launch="leadchat://chats/{conversation_id}" activationType="protocol">
  <visual>
    <binding template="ToastGeneric">
      <text>Иван Петров · Ремонт iPhone 13</text>
      <text>А сколько будет стоить замена экрана?</text>
      <text placement="attribution">LP-Москва</text>
    </binding>
  </visual>
  <actions>
    <input id="replyText" type="text" placeHolderContent="Быстрый ответ…"/>
    <action content="Ответить" activationType="background"
            arguments="action=reply&amp;conv={conversation_id}" hint-inputId="replyText"/>
    <action content="Открыть" activationType="protocol"
            arguments="leadchat://chats/{conversation_id}"/>
  </actions>
  <audio src="ms-winsoundevent:Notification.IM"/>
</toast>
```

- **Клик по телу тоста** и кнопка «Открыть» — protocol-активация `leadchat://chats/{id}`: Windows запускает/фокусирует приложение, deep-link (через single-instance, если процесс жив) превращается в событие `navigate`, роутер фронта открывает `/chats/{id}`, фокус — в поле ввода.
- **Кнопка «Ответить» с инлайн-полем** — background-активация. Для Win32-приложения она требует: (а) ярлык в Start Menu с AUMID (NSIS-инсталлятор Tauri его создаёт; AUMID = `ru.partner-lead-centre.leadchat`), (б) COM-активатор `INotificationActivationCallback`, зарегистрированный по CLSID в `HKCU\Software\Classes\CLSID\{...}` + `ToastActivatorCLSID` у ярлыка. Регистрацию делает `notify::init()` при старте (идемпотентно, HKCU — прав не требует). Активатор получает `arguments` и введённый текст из `replyText` и вызывает тот же путь, что офлайн-очередь:

```rust
// notify.rs — обработчик активации (упрощённо)
fn on_toast_activated(args: &str, user_input: Option<&str>, app: &AppHandle) {
    let conv = parse_arg(args, "conv");
    match parse_arg(args, "action").as_deref() {
        Some("reply") => {
            if let Some(text) = user_input.filter(|t| !t.trim().is_empty()) {
                // Быстрый ответ идёт ЧЕРЕЗ OUTBOX (разд. 5.3): мгновенно ставится
                // в очередь и уходит первым же flush — работает даже без сети.
                cache::outbox::push(app, &conv, "message", text);
                tauri::async_runtime::spawn(cache::outbox::flush(app.clone()));
            } else {
                tray::show_main(app);                 // пустой ответ → просто открыть
                let _ = app.emit("navigate", format!("/chats/{conv}"));
            }
        }
        _ => { tray::show_main(app); let _ = app.emit("navigate", format!("/chats/{conv}")); }
    }
}
```

> **Этапность (важно для оценки в 1.5 недели):** COM-активатор — самая трудоёмкая часть. В MVP этапа 4 допустимо собрать тост, где обе кнопки — protocol-активация («Ответить» просто открывает диалог с фокусом в поле ввода), а background-активацию с инлайн-текстом включить второй итерацией. XML и `notify_reply` от этого не меняются — меняется только `activationType` кнопки.

Тосты помечаются `tag = conversation_id`, `group = "leadchat"`: новое сообщение того же диалога **заменяет** его тост в Action Center, а не плодит стопку.

### 4.3. Троттлинг при потоке сообщений

Правила в `notifier.ts` (константы — в одном объекте, чтобы крутить без поиска по коду):

```ts
const THROTTLE = {
  perDialogCooldownMs: 30_000,  // не чаще 1 тоста на диалог в 30 с (замена по tag — не в счёт)
  globalWindowMs: 10_000,
  globalMax: 3,                 // >3 тостов за 10 с → переключаемся на сводный режим
  summaryEveryMs: 60_000,       // в сводном режиме — 1 тост в минуту:
                                // «12 новых сообщений в 5 диалогах», кнопка «Открыть LeadChat»
  quietResumeMs: 30_000,        // 30 с без новых событий → выход из сводного режима
};
```

Сводный тост использует `tag = "summary"` (обновляется, а не копится) и protocol-активацию на `leadchat://chats` (просто развернуть список). Звук — только у первого тоста серии (`<audio silent="true"/>` у последующих). Handoff-уведомления («передан вам») троттлингу не подчиняются.

---

## 5. Офлайн-режим

### 5.1. Принципы

1. Кэш — **проекция сервера, только для чтения**. Единственные локально-авторитетные данные — очередь `outbox`.
2. **Сервер всегда прав**: при любом расхождении локальная строка перезаписывается серверной. Никаких merge.
3. Кэш ограничен: **200 диалогов** по `last_message_at`, до **100 последних сообщений** на диалог (константы `CACHE_DIALOGS=200`, `CACHE_MSGS_PER_DIALOG=100`); prune после каждой синхронизации.
4. Всё содержательное (тексты, имена, телефоны) хранится **зашифрованным** (разд. 5.5); JS не имеет доступа к открытому тексту иначе как через Rust-команды.

### 5.2. Схема локального SQLite (`migrations/0001_init.sql`)

```sql
PRAGMA journal_mode = WAL;

CREATE TABLE meta (                     -- служебное состояние
  key   TEXT PRIMARY KEY,               -- 'last_sync_at' | 'user_id' | 'schema_ver' | ...
  value TEXT NOT NULL
);

CREATE TABLE dialogs (                  -- проекция conversations сервера
  id              TEXT PRIMARY KEY,     -- conversations.id (uuid) с сервера
  account_id      TEXT NOT NULL,        -- avito_accounts.id
  account_title   TEXT NOT NULL,        -- «LP-Москва» — для списка без JOIN'ов
  status          TEXT NOT NULL,        -- new | in_progress | closed (как на сервере)
  assignee_id     TEXT,
  assignee_name   TEXT,
  unread          INTEGER NOT NULL DEFAULT 0,
  last_message_at TEXT,                 -- ISO-8601 UTC
  updated_at      TEXT NOT NULL,        -- серверный updated_at — курсор синка
  client_name_enc BLOB,                 -- AES-256-GCM (nonce||ciphertext)
  client_phone_enc BLOB,
  item_title_enc  BLOB,                 -- название объявления тоже считаем содержательным
  card_json_enc   BLOB                  -- полный JSON карточки (клиент, объявление, заметки)
);
CREATE INDEX idx_dialogs_lastmsg ON dialogs(last_message_at DESC);

CREATE TABLE cached_messages (          -- проекция messages сервера
  id             TEXT PRIMARY KEY,      -- messages.id с сервера
  dialog_id      TEXT NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
  direction      TEXT NOT NULL,         -- in | out | note | system   (как в messages)
  sender_type    TEXT NOT NULL,         -- client | operator | bot | system
  sender_name    TEXT,
  body_enc       BLOB,
  attachments_enc BLOB,                 -- JSON-метаданные вложений (сами файлы не кэшируем)
  delivery_status TEXT NOT NULL DEFAULT 'delivered',
  created_at     TEXT NOT NULL
);
CREATE INDEX idx_cmsgs_dialog ON cached_messages(dialog_id, created_at);

CREATE TABLE outbox (                   -- исходящая очередь (локально-авторитетная)
  client_msg_id TEXT PRIMARY KEY,       -- uuid v4, генерируется при наборе
  dialog_id     TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT 'message',   -- message | note
  body_enc      BLOB NOT NULL,
  queued_at     TEXT NOT NULL,
  attempts      INTEGER NOT NULL DEFAULT 0,
  next_try_at   TEXT,                   -- экспоненциальный backoff
  last_error    TEXT,
  status        TEXT NOT NULL DEFAULT 'queued'     -- queued | sending | failed
);
CREATE INDEX idx_outbox_status ON outbox(status, next_try_at);
```

Вложения не кэшируются (только метаданные): media отдаётся nginx по подписанным ссылкам (DESIGN 1.4), в офлайне вместо картинки — плейсхолдер «доступно при подключении».

### 5.3. Outbox: отправка, набранная в офлайне

Путь сообщения в десктопе **всегда** один и тот же — через outbox (и онлайн, и офлайн; это убирает отдельную ветку кода):

1. Оператор жмёт «Отправить» → фронт вызывает `outbox_push({dialog_id, kind, body})` → Rust генерирует `client_msg_id`, шифрует body, пишет строку, возвращает `client_msg_id`. UI сразу рисует сообщение с меткой ⏳ (DESIGN 3.6).
2. `outbox_flush` запускается: сразу после `push`; при событии reconnect WS; при `navigator.onLine → true`; таймером раз в 30 с, если есть `queued`.
3. Flush шлёт по одному, в порядке `queued_at`:
   `POST /api/v1/conversations/{dialog_id}/messages` (для `kind='note'` —
   `POST /api/v1/conversations/{dialog_id}/notes`, 01 §6.4) с телом
   `{ "text": ..., "client_message_id": "<client_msg_id>" }` (идемпотентность — 01 §1.6),
   с access-JWT текущей сессии.
4. Результаты:
   - **2xx** — строка удаляется из outbox; сервер вернул полноценный `message` → он попадёт в кэш и в UI (⏳ → ✓) через обычный WS/синк;
   - **401** — фронт обновляет токен (стандартный refresh-флоу), flush повторяется;
   - **409/422/403** (диалог закрыт, роль без права `messages:send` и т.п.) — `status='failed'` + `last_error`; в UI сообщение краснеет с кнопкой «Повторить»/«Удалить»;
   - **5xx/сеть/таймаут** — `attempts+1`, `next_try_at = now + min(2^attempts, 300)` секунд; после 10 попыток — `failed` (ручной повтор сбрасывает счётчик).

> **Контракт бэкенда (01-API-SPEC §1.6):** отправка идемпотентна по `client_message_id` в теле
> (Redis `SET NX idem:msg:{conversation_id}:{client_message_id}`, TTL 24 ч); повторный запрос
> с тем же id возвращает уже созданное сообщение с `200`, а не создаёт дубль. Без этого ретраи
> outbox дублировали бы сообщения клиентам.

### 5.4. Синхронизация при старте и reconnect

```
Старт приложения
  1. cache_load_dialogs() → UI отрисован из SQLite мгновенно (< 100 мс), офлайн-бейдж
  2. Параллельно: GET /api/v1/conversations?limit=200
       (сортировка фиксированная серверная, 01 §5.1 — limit=200 даёт нужный топ-200)
       → cache_apply_sync(): upsert всех диалогов, СЕРВЕР ПРАВ (перезапись),
         диалоги, пропавшие из топ-200, удаляются из кэша (prune)
  3. Для открытого диалога: GET /api/v1/conversations/{id}/messages?limit=100 → upsert
  4. outbox_flush()
  5. meta.last_sync_at = серверное время из ответа

Reconnect WS (сеть моргнула)
  1. GET /api/v1/conversations?updated_since={last_sync_at}&limit=200
       → дельта-апдейт кэша (дешевле полного)
  2. Для диалогов из дельты, открытых сейчас, — догрузка сообщений
  3. outbox_flush()
```

- Сообщения кэшируются лениво: диалог открыли → последние 100 сообщений ушли в `cached_messages`. Прокрутка глубже 100 в офлайне недоступна (плашка «история доступна при подключении»).
- **Разрешение конфликтов — тривиальное по построению**: локальных правок серверных сущностей не существует (статусы, назначения, заметки в офлайне задизейблены в UI, кроме набора сообщений), поэтому «конфликт» — это всегда «перезаписать локальное серверным». Единственный содержательный случай: сообщение из outbox уже доставлено (через `client_message_id`), а ответ 2xx потерялся — тогда при синке пришедшее с сервера сообщение и локальная ⏳-строка дедуплицируются: сервер возвращает `client_message_id` в теле `message` (01 §6.1), кэш убирает outbox-строку.
- `updated_since` на `GET /api/v1/conversations` — контракт 01 §5.1 (работает по `conversations.updated_at`; отдаёт и закрытые/переназначенные, чтобы кэш узнал об изменениях).

### 5.5. Шифрование кэша: DPAPI + AES-256-GCM

Схема двухуровневая (стандартная для Windows-приложений):

```
random 32 байта (мастер-ключ, генерируется при первом запуске)
        │ CryptProtectData (DPAPI, scope: current user, entropy: константа приложения)
        ▼
%APPDATA%\ru.partner-lead-centre.leadchat\cache.key   (DPAPI-блоб на диске)

Каждое значение: nonce(12) || AES-256-GCM(мастер-ключ, значение)  → BLOB *_enc в SQLite
```

Почему так, а не шифрование файла целиком: `tauri-plugin-sql` (sqlx) не поддерживает SQLCipher, а связка «DPAPI-обёрнутый мастер-ключ + шифрование содержательных колонок» даёт тот же результат (файл кэша бесполезен без профиля Windows этого пользователя) без смены плагина из DESIGN.md. Расшифрованный мастер-ключ живёт только в памяти Rust-процесса, в JS не передаётся никогда.

```rust
// src-tauri/src/cache/dpapi.rs
use windows::Win32::Foundation::LocalFree;
use windows::Win32::Security::Cryptography::{
    CryptProtectData, CryptUnprotectData, CRYPT_INTEGER_BLOB, CRYPTPROTECT_UI_FORBIDDEN,
};

const ENTROPY: &[u8] = b"leadchat.cache.v1";   // связывает блоб с нашим приложением

fn blob(data: &[u8]) -> CRYPT_INTEGER_BLOB {
    CRYPT_INTEGER_BLOB { cbData: data.len() as u32, pbData: data.as_ptr() as *mut u8 }
}

pub fn protect(plain: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = CRYPT_INTEGER_BLOB::default();
    unsafe {
        CryptProtectData(&blob(plain), None, Some(&blob(ENTROPY)), None, None,
                         CRYPTPROTECT_UI_FORBIDDEN, &mut out)
            .map_err(|e| e.to_string())?;                    // CHECK: сигнатура windows 0.58
        let v = std::slice::from_raw_parts(out.pbData, out.cbData as usize).to_vec();
        let _ = LocalFree(Some(windows::Win32::Foundation::HLOCAL(out.pbData as _)));
        Ok(v)
    }
}

pub fn unprotect(cipher: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = CRYPT_INTEGER_BLOB::default();
    unsafe {
        CryptUnprotectData(&blob(cipher), None, Some(&blob(ENTROPY)), None, None,
                           CRYPTPROTECT_UI_FORBIDDEN, &mut out)
            .map_err(|e| e.to_string())?;
        let v = std::slice::from_raw_parts(out.pbData, out.cbData as usize).to_vec();
        let _ = LocalFree(Some(windows::Win32::Foundation::HLOCAL(out.pbData as _)));
        Ok(v)
    }
}
```

```rust
// src-tauri/src/cache/mod.rs — инициализация при старте
pub fn init(app: &AppHandle) -> Result<(), String> {
    let dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    std::fs::create_dir_all(&dir).ok();
    let key_path = dir.join("cache.key");

    let master: [u8; 32] = if key_path.exists() {
        let wrapped = std::fs::read(&key_path).map_err(|e| e.to_string())?;
        match dpapi::unprotect(&wrapped) {
            Ok(k) => k.try_into().map_err(|_| "bad key len")?,
            Err(_) => {
                // Профиль переехал/переустановлен → расшифровать нельзя.
                // Кэш — всего лишь проекция сервера: молча пересоздаём с нуля.
                std::fs::remove_file(dir.join("cache.db")).ok();
                new_master_key(&key_path)?
            }
        }
    } else { new_master_key(&key_path)? };

    app.manage(CacheState::open(dir.join("cache.db"), master)?);  // rusqlite + миграции
    Ok(())
}

fn new_master_key(path: &std::path::Path) -> Result<[u8; 32], String> {
    use rand::RngCore;
    let mut key = [0u8; 32];
    rand::rngs::OsRng.fill_bytes(&mut key);
    std::fs::write(path, dpapi::protect(&key)?).map_err(|e| e.to_string())?;
    Ok(key)
}
```

`cache/crypto.rs` — тонкая обёртка `aes-gcm`: `encrypt(v) = nonce(12, случайный) || seal(...)`, `decrypt` — соответственно. Outbox `attempts >= 1` не расшифровывается на JS-стороне вообще — flush работает целиком в Rust.

Refresh-токен сессии в файлы **не выгружается**: десктоп живёт на том же httpOnly-cookie
WebView2, что и веб (DESIGN §9; 01 §1.2 — «в теле ответов refresh не появляется никогда»),
cookie персистентна в user data folder WebView2. Rust-команды, ходящие в API сами
(`outbox_flush`), получают текущий access-JWT от фронта командой `set_session_token`
(значение живёт только в памяти процесса), а на `401` эмитят `session:refresh-needed` —
фронт делает обычный refresh по cookie и передаёт новый токен. Команды
`dpapi_encrypt`/`dpapi_decrypt` (разд. 8) остаются для служебных данных кэша.

---

## 6. Автообновление

### 6.1. Канал и раскладка на сервере

Канал один — **stable**; имя манифеста фиксировано: `latest.json`. (Если появится beta-канал — это второй файл `beta.json` и опция в настройках, endpoint подставляется при старте; в архитектуре ничего не меняется.)

```
/var/leadchat/download/                    ← пишет CI, читает nginx
├── latest.json                            ← манифест updater (stable)
├── LeadChat_1.4.2_x64-setup.exe           ← NSIS: сотрудники + автообновление
├── LeadChat_1.4.2_x64-setup.exe.sig       ← minisign-подпись (createUpdaterArtifacts)
├── LeadChat_1.4.2_x64_ru-RU.msi           ← MSI: развёртывание через GPO
├── LeadChat_1.4.2_x64_ru-RU.msi.sig
└── LeadChat-Setup.exe                     ← стабильное имя (символическая ссылка
                                             на свежий setup.exe) для кнопки на /login
```

nginx (фрагмент серверного конфига, полный — в 05-DEPLOY):

```nginx
location /download/ {
    alias /var/leadchat/download/;
    autoindex off;
    location ~ \.json$ { add_header Cache-Control "no-cache"; }
    location ~ \.(exe|msi|sig)$ { add_header Cache-Control "public, max-age=3600"; }
}
location = /download { return 302 /download/LeadChat-Setup.exe; }
```

`latest.json` (формат Tauri 2 updater):

```json
{
  "version": "1.4.2",
  "notes": "Исправлен бейдж непрочитанных; быстрый ответ из уведомления",
  "pub_date": "2026-08-04T12:00:00Z",
  "platforms": {
    "windows-x86_64": {
      "signature": "<содержимое LeadChat_1.4.2_x64-setup.exe.sig>",
      "url": "https://chat.partner-lead-centre.ru/download/LeadChat_1.4.2_x64-setup.exe"
    }
  }
}
```

Обновление ездит на NSIS-артефакте. MSI-установки (GPO) обновляются централизованно новой версией MSI — updater в них не смешивается (смешение форматов NSIS/MSI при апдейте не поддерживается Tauri).

### 6.2. Подпись (minisign)

```bash
# один раз, локально у ответственного за релизы:
npm run tauri signer generate -- -w ~/.tauri/leadchat.key
# → приватный ключ (+ парольная фраза) и публичный ключ
```

- **Публичный ключ** → `plugins.updater.pubkey` в `tauri.conf.json` (коммитится).
- **Приватный ключ и пароль** → GitHub Secrets `TAURI_SIGNING_PRIVATE_KEY`, `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`; больше нигде (ни в репо, ни на VPS — сервер только раздаёт файлы, подписывать ему нечем; компрометация VPS не даёт подсунуть обновление, т.к. клиент проверяет подпись зашитым pubkey).
- Ротация ключа = выпуск версии, подписанной старым ключом, с новым pubkey в конфиге, затем переход.

Отдельный вопрос — **Authenticode/SmartScreen**: minisign защищает канал обновлений, но первый запуск скачанного инсталлятора Windows встретит предупреждением SmartScreen (у нас нет EV-сертификата). Для внутреннего инструмента это приемлемо: в онбординг-инструкции — «Подробнее → Выполнить в любом случае»; при желании ИТ добавляет self-signed сертификат в доверенные через GPO. Покупка сертификата — опция, не блокер.

### 6.3. Клиентский флоу

```ts
// frontend/src/platform/tauri/updater.ts
import { check } from "@tauri-apps/plugin-updater";
import { relaunch } from "@tauri-apps/plugin-process";

export async function checkForUpdates(notify: (u: UpdateBanner) => void) {
  try {
    const update = await check();          // GET latest.json + сравнение версий
    if (!update) return;
    notify({
      version: update.version,
      notes: update.body,
      apply: async () => {
        // На Windows: скачивает setup.exe, проверяет minisign-подпись,
        // запускает NSIS в passive-режиме (installMode из tauri.conf.json),
        // приложение закрывается само; открытый outbox не теряется (SQLite).
        await update.downloadAndInstall();
        await relaunch();                  // CHECK: на Windows NSIS сам перезапускает
      },
    });
  } catch { /* нет сети/сервера — молча, попробуем в следующий раз */ }
}
// Расписание: при старте (через 30 с после запуска, чтобы не мешать синку)
// и далее каждые 4 часа таймером.
```

Правила UX: обновление **никогда не ставится без клика** — показывается ненавязчивый баннер «Доступна версия 1.4.2 — Перезапустить», т.к. у оператора может быть открыт недописанный ответ. Если баннер игнорируют 24 часа — повторный показ при следующем старте. Версия и канал видны в `/settings/profile` («О приложении»).

---

## 7. CI: сборка и публикация (GitHub Actions)

Релиз десктопа отвязан от релизов бэкенда: тег `desktop-v*`.

```yaml
# .github/workflows/desktop-release.yml
name: desktop-release

on:
  push:
    tags: ["desktop-v*"]        # desktop-v1.4.2
  workflow_dispatch:            # ручной запуск для проверки сборки (без публикации)

env:
  VITE_API_BASE: https://chat.partner-lead-centre.ru
  VITE_IS_DESKTOP: "1"

jobs:
  build:
    runs-on: windows-latest
    environment: desktop-release          # secrets доступны только этому environment
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: npm
          cache-dependency-path: |
            frontend/package-lock.json
            desktop/package-lock.json

      - uses: dtolnay/rust-toolchain@stable
        with:
          targets: x86_64-pc-windows-msvc

      - uses: swatinem/rust-cache@v2
        with:
          workspaces: desktop/src-tauri

      - name: Install deps
        run: |
          npm ci --prefix frontend
          npm ci --prefix desktop

      - name: Check version matches tag
        shell: bash
        run: |
          TAG_VER="${GITHUB_REF_NAME#desktop-v}"
          CONF_VER=$(node -p "require('./desktop/src-tauri/tauri.conf.json').version")
          if [ "${GITHUB_REF_TYPE}" = "tag" ] && [ "$TAG_VER" != "$CONF_VER" ]; then
            echo "tauri.conf.json version=$CONF_VER != tag $TAG_VER"; exit 1
          fi

      - name: Build (NSIS + MSI, updater artifacts)
        run: npm run --prefix desktop tauri build
        env:
          TAURI_SIGNING_PRIVATE_KEY: ${{ secrets.TAURI_SIGNING_PRIVATE_KEY }}
          TAURI_SIGNING_PRIVATE_KEY_PASSWORD: ${{ secrets.TAURI_SIGNING_PRIVATE_KEY_PASSWORD }}

      - name: Generate latest.json
        shell: bash
        run: node desktop/scripts/make-latest-json.mjs   # см. ниже

      - uses: actions/upload-artifact@v4
        with:
          name: leadchat-desktop
          path: |
            desktop/src-tauri/target/release/bundle/nsis/*.exe
            desktop/src-tauri/target/release/bundle/nsis/*.sig
            desktop/src-tauri/target/release/bundle/msi/*.msi
            desktop/src-tauri/target/release/bundle/msi/*.sig
            dist-release/latest.json

  publish:
    needs: build
    if: startsWith(github.ref, 'refs/tags/desktop-v')
    runs-on: ubuntu-latest                # выкладка — с linux-раннера (ssh/rsync проще)
    environment: desktop-release
    steps:
      - uses: actions/download-artifact@v4
        with: { name: leadchat-desktop, path: out }

      - name: Setup SSH
        run: |
          mkdir -p ~/.ssh
          echo "${{ secrets.DEPLOY_SSH_KEY }}" > ~/.ssh/id_ed25519
          chmod 600 ~/.ssh/id_ed25519
          ssh-keyscan -H chat.partner-lead-centre.ru >> ~/.ssh/known_hosts

      - name: Upload artifacts, then manifest (порядок важен)
        run: |
          HOST=deploy@chat.partner-lead-centre.ru
          DIR=/var/leadchat/download
          # 1) сначала бинарники и подписи…
          rsync -av --include='*.exe' --include='*.sig' --include='*.msi' \
                --exclude='latest.json' out/ $HOST:$DIR/
          # 2) обновить стабильную ссылку для страницы /login
          SETUP=$(basename out/**/nsis/*-setup.exe)
          ssh $HOST "ln -sf $DIR/$SETUP $DIR/LeadChat-Setup.exe"
          # 3) …и только потом манифест (атомарно): клиенты никогда не увидят
          #    latest.json, указывающий на ещё не выложенный файл
          rsync -av out/dist-release/latest.json $HOST:$DIR/latest.json.new
          ssh $HOST "mv $DIR/latest.json.new $DIR/latest.json"
```

```js
// desktop/scripts/make-latest-json.mjs
import { readFileSync, writeFileSync, mkdirSync, readdirSync } from "node:fs";
import { join } from "node:path";

const conf = JSON.parse(readFileSync("desktop/src-tauri/tauri.conf.json", "utf8"));
const nsisDir = "desktop/src-tauri/target/release/bundle/nsis";
const setup = readdirSync(nsisDir).find(f => f.endsWith("-setup.exe"));
const sig = readFileSync(join(nsisDir, setup + ".sig"), "utf8");

mkdirSync("dist-release", { recursive: true });
writeFileSync("dist-release/latest.json", JSON.stringify({
  version: conf.version,
  notes: process.env.RELEASE_NOTES ?? "",
  pub_date: new Date().toISOString(),
  platforms: {
    "windows-x86_64": {
      signature: sig.trim(),
      url: `https://chat.partner-lead-centre.ru/download/${setup}`,
    },
  },
}, null, 2));
```

Secrets в environment `desktop-release`: `TAURI_SIGNING_PRIVATE_KEY`, `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`, `DEPLOY_SSH_KEY` (отдельный пользователь `deploy` на VPS, доступ только к `/var/leadchat/download`, shell-команды ограничены при желании через `authorized_keys` `command=`). PR-ветки собирают десктоп без подписи и без публикации (обычный CI-workflow с `tauri build --debug` — не в этом файле).

Чек-лист релиза: поднять `version` в `tauri.conf.json` (и `Cargo.toml`) → PR → merge → `git tag desktop-v1.4.2 && git push --tags` → через ~15 минут клиенты видят баннер обновления.

---

## 8. Rust-команды (`#[tauri::command]`)

### 8.1. Сводная таблица

| Команда | Сигнатура (Rust) | Назначение |
|---|---|---|
| `set_badge` | `fn set_badge(app: AppHandle, count: u32) -> Result<(), String>` | Иконка трея + overlay таскбара + пункт меню «Новые чаты: N» (разд. 3.2) |
| `notify_reply` | `async fn notify_reply(app: AppHandle, req: ToastRequest) -> Result<(), String>` | WinRT-тост с кнопкой «Ответить» и инлайн-полем (разд. 4.2) |
| `outbox_push` | `async fn outbox_push(state: State<'_, CacheState>, dialog_id: String, kind: String, body: String) -> Result<String, String>` | Поставить исходящее в очередь; возвращает `client_msg_id` (разд. 5.3) |
| `outbox_flush` | `async fn outbox_flush(app: AppHandle, state: State<'_, CacheState>) -> Result<FlushReport, String>` | Прогнать очередь: POST с `client_message_id` в теле (01 §1.6), backoff, статусы; отчёт для UI |
| `set_session_token` | `fn set_session_token(state: State<'_, CacheState>, token: String) -> Result<(), String>` | Принять от фронта текущий access-JWT для Rust-запросов (`outbox_flush`); только в памяти (разд. 5.5) |
| `dpapi_encrypt` | `fn dpapi_encrypt(data: Vec<u8>) -> Result<Vec<u8>, String>` | DPAPI-шифрование произвольного блоба (служебные данные кэша) |
| `dpapi_decrypt` | `fn dpapi_decrypt(data: Vec<u8>) -> Result<Vec<u8>, String>` | Обратная операция |
| `cache_apply_sync` | `async fn cache_apply_sync(state: State<'_, CacheState>, dialogs: Vec<DialogSync>, messages: Vec<MessageSync>) -> Result<(), String>` | Upsert серверных данных в кэш (шифрование колонок внутри) + prune 200/100 |
| `cache_load_dialogs` | `async fn cache_load_dialogs(state: State<'_, CacheState>) -> Result<Vec<DialogView>, String>` | Расшифрованный список диалогов для мгновенного старта |
| `cache_load_messages` | `async fn cache_load_messages(state: State<'_, CacheState>, dialog_id: String, limit: u32) -> Result<Vec<MessageView>, String>` | Расшифрованные сообщения диалога (+ ⏳-строки из outbox) |
| `set_presence` | `fn set_presence(app: AppHandle, status: String) -> Result<(), String>` | Синхронизировать галки статуса в трее, когда статус сменили из UI |

```rust
#[derive(serde::Deserialize)]
pub struct ToastRequest {
    pub conversation_id: String,
    pub title: String,        // «Иван Петров · Ремонт iPhone 13»
    pub body: String,         // текст сообщения (обрезан фронтом до ~120 символов)
    pub attribution: String,  // «LP-Москва» — аккаунт-получатель
    pub silent: bool,         // троттлинг: без звука
    pub summary: bool,        // сводный тост (tag="summary", без кнопки «Ответить»)
}

#[derive(serde::Serialize)]
pub struct FlushReport {
    pub sent: u32,
    pub failed: Vec<FailedItem>,   // {client_msg_id, dialog_id, error}
    pub remaining: u32,            // осталось queued (нет сети)
}
```

### 8.2. Эскиз `outbox_flush`

```rust
#[tauri::command]
pub async fn outbox_flush(
    app: tauri::AppHandle,
    state: tauri::State<'_, CacheState>,
) -> Result<FlushReport, String> {
    // защита от параллельных flush (таймер + reconnect могут совпасть)
    let _guard = state.flush_lock.try_lock().map_err(|_| "flush already running")?;
    let mut report = FlushReport::default();

    for item in state.outbox_due()? {                 // status=queued AND next_try_at<=now
        let body = state.crypto.decrypt(&item.body_enc)?;   // только в памяти Rust
        let path = if item.kind == "note" { "notes" } else { "messages" };  // 01 §6.2/§6.4
        let resp = state.http
            .post(format!("{}/api/v1/conversations/{}/{}", API_BASE, item.dialog_id, path))
            .bearer_auth(state.session_token()?)       // access-JWT от фронта (set_session_token)
            .json(&serde_json::json!({ "text": String::from_utf8_lossy(&body),
                                        "client_message_id": item.client_msg_id }))
            .timeout(std::time::Duration::from_secs(15))
            .send().await;

        match resp {
            Ok(r) if r.status().is_success() => {
                state.outbox_delete(&item.client_msg_id)?;
                report.sent += 1;
            }
            Ok(r) if r.status() == 401 => {            // пусть фронт обновит токен
                let _ = app.emit("session:refresh-needed", ());
                break;
            }
            Ok(r) if r.status().is_client_error() => { // 403/409/422 — не ретраить
                state.outbox_fail(&item.client_msg_id, &format!("HTTP {}", r.status()))?;
                report.failed.push(item.as_failed(r.status()));
            }
            _ => {                                     // сеть/5xx — backoff
                state.outbox_retry_later(&item.client_msg_id)?;   // 2^attempts, cap 300 c
                report.remaining += 1;
                break;                                 // сети нет — остальные не трогаем
            }
        }
    }
    let _ = app.emit("outbox:report", &report);        // UI обновляет ⏳/✗
    Ok(report)
}
```

### 8.3. Capabilities (`src-tauri/capabilities/main.json`)

Без этого файла в Tauri 2 фронту не доступен ни один API. Минимально необходимое:

```json
{
  "$schema": "../gen/schemas/desktop-schema.json",
  "identifier": "main-capability",
  "windows": ["main"],
  "permissions": [
    "core:default",
    "core:window:allow-show", "core:window:allow-hide",
    "core:window:allow-set-focus", "core:window:allow-unminimize",
    "core:event:allow-listen", "core:event:allow-emit",
    "notification:default",
    "autostart:allow-enable", "autostart:allow-disable", "autostart:allow-is-enabled",
    "sql:allow-execute", "sql:allow-select",
    "updater:default",
    "process:allow-restart",
    "deep-link:default"
  ]
}
```

Заметьте: `global-shortcut` в списке отсутствует — хоткей регистрируется в Rust при старте, JS-доступ к регистрации шорткатов не нужен. Собственные команды из 8.1 разрешены по умолчанию для окна `main` (`core:default` включает invoke своих команд приложения).

---

## 9. Зависимости от других частей системы

Что десктоп требует от бэкенда (всё зафиксировано в 01-API-SPEC):

1. **Идемпотентность по `client_message_id`** в теле `POST /api/v1/conversations/{id}/messages` (и `/notes`) — 01 §1.6; без неё outbox дублирует сообщения (5.3).
2. **`client_message_id`** в ответе (`MessageOut`) и в WS-событии `message:new` для дедупликации ⏳-строк (5.4) — 01 §6.1.
3. **`?updated_since=`** на `GET /api/v1/conversations` — дельта-синхронизация (5.4) — 01 §5.1 (по колонке `conversations.updated_at`).
4. **Presence-endpoint** `PUT /api/v1/presence` со статусом `online|away` (01 §11.6) — статус из трея; отражается онлайн-индикатором в `/settings/team` (DESIGN 3.5).
5. Раздача `/download/` в nginx (6.1) и ссылка «Скачать приложение для Windows» на `/login` (DESIGN 3.2) → `https://chat.partner-lead-centre.ru/download`.

## 10. Приёмка этапа 4 (definition of done)

- [ ] NSIS-инсталлятор ставится под обычным пользователем Windows 10/11 без UAC; размер ≤ 10 МБ.
- [ ] Логин, чаты, отправка — идентичны вебу (это тот же бандл); WS живёт при свёрнутом окне.
- [ ] Крестик сворачивает в трей; «Выход» из трея завершает процесс; повторный запуск ярлыком разворачивает существующее окно (single-instance).
- [ ] Бейдж на трее и таскбаре соответствует числу непрочитанных, обнуляется при прочтении.
- [ ] Тост при новом сообщении в свёрнутом состоянии; клик открывает нужный диалог; при потоке 20 сообщений/10 с — не более 3 тостов + сводный.
- [ ] Выдернуть сеть → диалоги читаются, отправленное помечено ⏳; вернуть сеть → ушло без дублей (проверить двойной retry с одним `client_message_id`).
- [ ] Файл `cache.db` с другого пользователя/машины не читается (колонки зашифрованы, `cache.key` не разворачивается чужим DPAPI).
- [ ] Выложить `latest.json` с версией +1 → в приложении баннер, обновление ставится, версия в «О приложении» новая; подмена `signature` → обновление отвергается.
- [ ] `Ctrl+Shift+L` и автозапуск с `--minimized` работают.
