# LeadChat Desktop (Windows, Tauri 2)

Десктоп-клиент LeadChat — **оболочка над тем же React-бандлом**, что и веб
(`frontend/`), а не второй фронтенд. Rust отвечает только за то, чего у веба быть
не может: трей и бейдж непрочитанных, тосты Windows, офлайн-кэш и очередь
исходящих, автозапуск, глобальный хоткей, автообновление и ссылки `leadchat://`.

Источник истины по требованиям — [`docs/04-DESKTOP.md`](../../docs/04-DESKTOP.md);
контракт с фронтом — `docs/03-FRONTEND.md` §7, API — `docs/01-API-SPEC.md`.

---

## ⚠️ Собрать инсталлятор можно только на Windows

Windows-специфика (WinRT-тосты, DPAPI, NSIS/WiX, оверлей таскбара) не собирается
и не запускается на macOS/Linux. Поэтому:

| Что | macOS / Linux | Windows |
|---|---|---|
| `cargo check` / `cargo test` (структура кода, тесты логики) | ✅ | ✅ |
| `npm run icons` (генерация иконок) | ✅ | ✅ |
| `npm run dev` (запуск приложения) | ⚠️ окно откроется, но трей/тосты/DPAPI — заглушки | ✅ |
| `npm run build` (NSIS + MSI + артефакты updater) | ❌ | ✅ |

Что уже проверено на macOS: `cargo check --all-targets`, `cargo test`
(86 тестов), `cargo clippy`. Windows-ветки на macOS не компилируются вовсе,
поэтому их API сверялись отдельно — пробным крейтом с теми же вызовами под
`cargo check --target x86_64-pc-windows-msvc` (чистый Rust: `tauri` + `windows`).
Целиком крейт так не проверить: `ring` и `libsqlite3-sys` тянут C-код и требуют
Windows SDK — это делает только CI на `windows-latest`.

Правило кода: **любой Windows-API — под `#[cfg(target_os = "windows")]`**, а
Windows-крейты (`windows`, `windows-sys`) — только в секции
`[target.'cfg(windows)'.dependencies]` в `src-tauri/Cargo.toml`. Для остальных
целей пишем честную заглушку (`Ok(())` или понятная ошибка), чтобы `cargo check`
на macOS проходил. Реальная сборка инсталлятора — CI на `windows-latest`
(04 §7, `.github/workflows/desktop-release.yml`).

---

## Что нужно на машине

**Общее:** Node 20+ (для CLI Tauri и генератора иконок), Rust stable
(`rustup`, ≥ 1.82).

**Windows (сборка):**

1. **Rust** с MSVC-таргетом: `rustup default stable-x86_64-pc-windows-msvc`.
2. **Visual Studio Build Tools 2022** с компонентом «Разработка классических
   приложений на C++» (нужен линкер `link.exe` и Windows SDK).
3. **WebView2 Runtime** — на Windows 10/11 уже есть; инсталлятор собран с
   `webviewInstallMode: downloadBootstrapper`, так что на голых машинах он
   доустановится сам (~5–8 МБ у самого инсталлятора).

**macOS/Linux (только проверка кода):** Rust stable + Xcode Command Line Tools
(или `build-essential`) — нужен C-компилятор для `libsqlite3-sys` (`bundled`).

---

## Запуск в dev

```bash
# один раз
npm ci --prefix ../frontend
npm ci --prefix .

# запуск: Vite поднимается сам (beforeDevCommand), Tauri цепляется к :5173
npm run dev
```

Фронт для десктопа собирается тем же `vite`, но в режиме `--mode desktop`:
абсолютный `VITE_API_BASE` (в Tauri origin — `tauri://localhost`, относительный
`/api` работать не будет) и флаг `VITE_IS_DESKTOP=1` (04 §1.1).

## Сборка релиза (только Windows)

```bash
npm run build             # NSIS + MSI + *.sig (createUpdaterArtifacts)
npm run build:debug       # то же без подписи и оптимизаций — для отладки
```

Артефакты: `src-tauri/target/release/bundle/nsis/*.exe(+.sig)` и
`.../msi/*.msi(+.sig)`. NSIS ставится **под текущего пользователя, без UAC**
(`installMode: currentUser`) — это основной вариант для сотрудников и именно он
участвует в автообновлении; MSI — для развёртывания через GPO/Intune (04 §6.1).

Полезное:

```bash
npm run check   # cargo check --all-targets (проходит и на macOS)
npm run test    # юнит-тесты Rust (deep-link, бейдж, presence)
npm run lint    # clippy -D warnings
npm run icons   # перегенерировать иконки
```

---

## Ключ автообновления (обязательно заменить)

В `tauri.conf.json` → `plugins.updater.pubkey` сейчас лежит **плейсхолдер**
(валидный по формату minisign-ключ из нулей — приложение с ним стартует, но
любое обновление будет отвергнуто, что безопасно). Перед первым релизом:

```bash
npm run tauri signer generate -- -w ~/.tauri/leadchat.key
```

* **публичный** ключ → `plugins.updater.pubkey` в `tauri.conf.json` (коммитится);
* **приватный** ключ и парольная фраза → GitHub Secrets
  `TAURI_SIGNING_PRIVATE_KEY` и `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`
  (environment `desktop-release`), больше нигде — ни в репозитории, ни на VPS
  (04 §6.2).

Endpoint обновлений — `https://chat.partner-lead-centre.ru/download/latest.json`
(раздаётся nginx, 05 §3). Пока продовый домен не переключён, десктоп ходит на тот
же адрес, что и веб: в CSP разрешены оба хоста — канонический домен и
`<адрес прода>` текущего стенда.

---

## Иконки

```bash
npm run icons     # node scripts/gen-tray-icons.mjs
```

Скрипт — **чистый Node без зависимостей и внешних бинарей** (свой растеризатор
с 4× суперсэмплингом, кодировщики PNG и ICO поверх `node:zlib`), поэтому
одинаково работает на macOS и на CI. Ни `sharp`, ни ImageMagick не нужны.

Что получается (всё коммитится в репозиторий — рантайм только выбирает файл,
никакой растеризации шрифтов в проде, 04 §3.2):

| Файл | Размер | Где используется |
|---|---|---|
| `icons/icon.ico` | 16/24/32/48/64/256 | иконка exe и инсталлятора |
| `icons/32x32.png`, `128x128.png`, `128x128@2x.png` | — | `bundle.icon` |
| `icons/tray/tray_0.png` | 32×32 | трей без непрочитанных (`app.trayIcon.iconPath`) |
| `icons/tray/tray_1..9.png`, `tray_9plus.png` | 32×32 | трей с числом |
| `icons/tray/badge_1..9.png`, `badge_9plus.png` | 16×16 | оверлей на кнопке таскбара |
| `icons/tray/badge_*@2x.png` | 32×32 | тот же оверлей для 150/200 % DPI |
| `icons/src/leadchat.svg` | — | дизайн-источник знака (совпадает с `favicon.svg`) |

Цвета — из дизайн-системы (10-UX §1.2): зелёный `#3cc13b`, чернила `#0c2e0c`;
красный бейджа `#e03131` (04 §3.2). Меняете знак — правьте и SVG, и константы в
скрипте, затем перегенерируйте и закоммитьте PNG.

Иконки трея **вшиты в бинарник** (`include_bytes!` в `src/tray.rs`), а не читаются
из `resource_dir()`: одинаково работает в dev и в установленном приложении, и не
зависит от того, попали ли файлы в ресурсы бандла.

---

## Что уже реализовано, а что — заглушки

`src/` (зона этого скелета):

| Файл | Что внутри | Док |
|---|---|---|
| `main.rs`, `lib.rs` | плагины, single-instance, deep-link, хоткей, автозапуск, close-to-tray, **регистрация всех команд** | §1.4 |
| `tray.rs` | меню трея, статус, бейдж, оверлей таскбара (`cfg(windows)`) | §3 |
| `commands.rs` | команды трея и окна | §8 |
| `notify.rs`, `toast_activator.rs` | тосты Windows, троттлинг, активация | §4 |
| `cache/*` | DPAPI-ключ, AES-GCM, SQLite-кэш, outbox | §5 |
| `sync.rs` | старт/реконнект-синхронизация, таймер flush | §5.4 |
| `updater.rs` | расписание проверок обновлений | §6 |

Каждая команда объявлена рядом со своей реализацией — иначе `#[tauri::command]`
порождает одноимённые макросы на весь крейт и сборка падает с `E0428`. Список
регистрации — один, в `lib.rs::run()`.

Ещё не сделано (не в этой папке): `frontend/src/platform/` — мост `PlatformBridge`,
notifier, offline и updater на стороне React (03 §7).

---

## Контракт с фронтом

**События Rust → фронт** (слушать через `@tauri-apps/api/event`):

| Событие | Payload | Когда |
|---|---|---|
| `navigate` | `"/chats/{id}"`, `"/chats/{id}?reply=1"` | deep-link `leadchat://…`, клик по тосту |
| `presence:set` | `"online"` \| `"away"` | статус переключили в меню трея → фронт делает `PUT /api/v1/presence` (01 §11.6) |
| `session:refresh-needed` | — | Rust получил 401 при отправке из очереди |
| `outbox:report` | `FlushReport` | прогон очереди завершён |
| `cache:synced` | `SyncReport` | синхронизация кэша прошла |
| `update:available` / `update:none` / `update:installing` / `update:error` | см. `updater.rs` | автообновление |

**Событие фронт → Rust:** `app:ready` — фронт эмитит его, когда навесил
слушатели. Нужно для холодного старта по ссылке из тоста: приложение ещё
запускалось, когда пришёл deep-link, — Rust придержит маршрут и отдаст его
сразу после `app:ready`. Без этого события первый `navigate` может уйти в пустоту.

**Команды** (`invoke`) — полный список в `lib.rs::run()`:

| Группа | Команды |
|---|---|
| трей и окно | `set_badge`, `toggle_main`, `set_presence` |
| уведомления | `notify_show`, `notify_reply` |
| сессия | `set_session_token`, `set_api_base` |
| кэш | `cache_load_dialogs`, `cache_load_messages`, `cache_apply_sync`, `cache_upsert_messages`, `cache_unread_total`, `cache_clear` |
| очередь | `outbox_push`, `outbox_flush`, `outbox_list`, `outbox_retry`, `outbox_delete` |
| синхронизация | `sync_startup`, `sync_reconnect`, `sync_dialog_messages`, `sync_last_at` |
| обновления | `updater_check`, `updater_install`, `updater_snooze`, `updater_status` |
| DPAPI | `dpapi_encrypt`, `dpapi_decrypt` |

> **Имена аргументов — `snake_case`.** По умолчанию Tauri 2 ждёт из JS camelCase;
> команды переключены на `rename_all = "snake_case"`, чтобы совпадать с REST-контрактом:
> `invoke("outbox_push", { dialog_id, kind, body })`, `invoke("set_badge", { count })`.

**npm-пакеты биндингов** ставятся во `frontend/package.json` (их импортирует
бандл, а не эта папка): `@tauri-apps/api`, `@tauri-apps/plugin-notification`,
`@tauri-apps/plugin-sql`, `@tauri-apps/plugin-updater`, `@tauri-apps/plugin-process`,
`@tauri-apps/plugin-autostart`, `@tauri-apps/plugin-opener`. Импорт — только
динамический (`await import(...)`), иначе Tauri протечёт в веб-бандл (03 §7).

---

## Permissions (capabilities)

`src-tauri/capabilities/main.json` — без него фронту не доступен **ни один**
Tauri-API (04 §8.3). Список намеренно узкий: у `sql` нет `allow-load`
(произвольные БД не открываем), а `global-shortcut` отсутствует вовсе — хоткей
регистрируется в Rust, из JS шорткаты вешать нельзя. Собственные команды
приложения разрешены `core:default`.

---

## Не забыть про `.gitignore`

Корневой `.gitignore` репозитория пока не знает про Rust. До того, как
кто-нибудь сделает первый коммит десктопа, туда нужно добавить:

```gitignore
desktop/src-tauri/target/
desktop/src-tauri/gen/
```

`target/` — это гигабайты артефактов сборки, `gen/` — схемы, которые
`tauri-build` генерирует заново при каждой сборке.
