//! Активация тостов Windows: как нажатие кнопки в тосте попадает в приложение.
//!
//! ## Что реализовано сейчас: ФОЛБЭК ПО 04 §4.2
//!
//! Документ прямо разрешает этапность: «в MVP этапа 4 допустимо собрать тост, где
//! обе кнопки — protocol-активация ("Ответить" просто открывает диалог с фокусом
//! в поле ввода), а background-активацию с инлайн-текстом включить второй
//! итерацией». Именно этот вариант **активен и собирается по умолчанию**:
//!
//! * клик по телу тоста и «Открыть» → `leadchat://chats/{id}`;
//! * «Ответить» → `leadchat://chats/{id}?reply=1` (окно разворачивается, роутер
//!   открывает диалог, фокус — в композер);
//! * поля ввода в тосте нет — без COM-активатора Windows физически некуда отдать
//!   набранный текст, поэтому пустое поле показывать нечестно.
//!
//! Путь ответа при этом уже полностью готов: `notify::notify_reply` кладёт текст
//! в outbox и работает офлайн — второй итерации остаётся только начать звать его
//! из COM-колбэка вместо фронта.
//!
//! ## Вторая итерация: настоящий COM-активатор
//!
//! Код `INotificationActivationCallback` ниже написан, но закрыт cargo-фичей
//! `toast-com-activator` и **по умолчанию не компилируется**: собрать и проверить
//! его можно только на Windows, а разработка идёт на macOS. Включение фичи
//! требует (см. cross-boundary заметки к спринту):
//!
//! 1. в `Cargo.toml` — `[features] toast-com-activator = []` и две недостающие
//!    фичи windows-зависимости: `"implement"` (сам макрос `#[implement]`) и
//!    `"Win32_UI_Shell"` (`INotificationActivationCallback`). `Win32_System_Com`
//!    и `Win32_System_Registry` там уже есть;
//! 2. в NSIS-шаблоне — свойство `System.AppUserModel.ToastActivatorCLSID` у
//!    ярлыка в меню «Пуск» (сам AUMID ярлыка Tauri проставляет);
//! 3. проверки на живой Windows-машине — сигнатуры помечены `// CHECK`.
//!
//! Пока фича выключена, `mode()` возвращает `Protocol`, и `notify.rs` строит XML
//! без `<input>`.

use tauri::AppHandle;

/// CLSID COM-активатора. Живёт в `HKCU\Software\Classes\CLSID\{...}` и в свойстве
/// ярлыка `System.AppUserModel.ToastActivatorCLSID`. Значение фиксировано:
/// менять его после релиза нельзя — старые ярлыки перестанут активировать тосты.
pub const TOAST_ACTIVATOR_CLSID: &str = "{4F8B2C3E-6D51-4A79-9C0B-1E7A5D2F8B64}";

/// То же значение числом — из него собирается `GUID` без парсинга строк.
/// Инвариант «строка == число» проверяется тестом.
pub const TOAST_ACTIVATOR_CLSID_U128: u128 = 0x4F8B_2C3E_6D51_4A79_9C0B_1E7A_5D2F_8B64;

/// Как тост доставляет действие в приложение.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ActivationMode {
    /// Фолбэк 04 §4.2: обе кнопки — protocol-активация `leadchat://`.
    Protocol,
    /// COM-активатор: кнопка «Ответить» с инлайн-полем (background-активация).
    ComCallback,
}

impl ActivationMode {
    pub fn is_com_callback(self) -> bool {
        matches!(self, ActivationMode::ComCallback)
    }

    pub fn describe(self) -> &'static str {
        match self {
            ActivationMode::Protocol => {
                "protocol-фолбэк (04 §4.2): «Ответить» открывает диалог, инлайн-поля нет"
            }
            ActivationMode::ComCallback => {
                "COM-активатор INotificationActivationCallback: инлайн-ответ из тоста"
            }
        }
    }
}

fn log(msg: &str) {
    eprintln!("[leadchat::toast_activator] {msg}");
}

/// Текущий режим активации.
pub fn mode() -> ActivationMode {
    #[cfg(all(target_os = "windows", feature = "toast-com-activator"))]
    {
        if com::is_registered() {
            return ActivationMode::ComCallback;
        }
    }
    ActivationMode::Protocol
}

/// Вызывается из `notify::init()` при старте. Идемпотентно.
///
/// В режиме фолбэка делать нечего: схему `leadchat://` регистрирует
/// `tauri-plugin-deep-link`, а доставку в живой процесс — `single-instance`.
pub fn init(app: &AppHandle) -> Result<(), String> {
    init_impl(app)
}

#[cfg(all(target_os = "windows", feature = "toast-com-activator"))]
fn init_impl(app: &AppHandle) -> Result<(), String> {
    com::init(app)
}

#[cfg(all(target_os = "windows", not(feature = "toast-com-activator")))]
fn init_impl(_app: &AppHandle) -> Result<(), String> {
    log("COM-активатор выключен (feature toast-com-activator) — работает protocol-фолбэк");
    Ok(())
}

#[cfg(not(target_os = "windows"))]
fn init_impl(_app: &AppHandle) -> Result<(), String> {
    log("не-Windows сборка: активация тостов не регистрируется (dev-режим)");
    Ok(())
}

/// Точка входа для protocol-активации: `deeplink.rs` отдаёт сюда URL
/// `leadchat://chats/{id}[?reply=1]`, полученный из тоста или из внешней ссылки.
///
/// Смысл прослойки — единая обработка активации: и COM-колбэк, и deep-link
/// заканчиваются одним и тем же `notify::on_toast_activated`.
pub fn handle_protocol_activation(app: &AppHandle, url: &str) {
    log(&format!("protocol-активация: {url}"));
    crate::notify::on_toast_activated(app, url, None);
}

// ---------------------------------------------------------------------------
// COM-активатор (вторая итерация; не собирается по умолчанию)
// ---------------------------------------------------------------------------

#[cfg(all(target_os = "windows", feature = "toast-com-activator"))]
mod com {
    //! Реализация `INotificationActivationCallback` (Win32-приложение + WinRT-тост).
    //!
    //! Схема работы Windows:
    //! 1. у ярлыка в меню «Пуск» есть AUMID и `ToastActivatorCLSID`;
    //! 2. пользователь жмёт «Ответить» в тосте с `activationType="background"`;
    //! 3. Windows поднимает COM-сервер по CLSID (или использует уже
    //!    зарегистрированный в живом процессе) и зовёт `Activate` с `arguments`
    //!    и массивом пар «id поля → введённый текст»;
    //! 4. мы достаём `replyText` и отдаём его в `notify::on_toast_activated`,
    //!    который кладёт ответ в outbox.
    //!
    //! ВСЕ сигнатуры помечены `// CHECK`: код не собирался (разработка на macOS),
    //! сверить по фактической версии crate `windows` при включении фичи.

    use std::ffi::c_void;
    use std::sync::atomic::{AtomicU32, Ordering};
    use std::sync::OnceLock;

    use tauri::AppHandle;
    use windows::core::{implement, IUnknown, Interface, Result as WinResult, GUID, PCWSTR};
    use windows::Win32::Foundation::{BOOL, E_INVALIDARG, E_POINTER};
    use windows::Win32::System::Com::{
        CoRegisterClassObject, IClassFactory, IClassFactory_Impl, CLSCTX_LOCAL_SERVER,
        REGCLS_MULTIPLEUSE,
    };
    use windows::Win32::System::Registry::{
        RegCloseKey, RegCreateKeyExW, RegSetValueExW, HKEY, HKEY_CURRENT_USER, KEY_WRITE,
        REG_OPTION_NON_VOLATILE, REG_SZ,
    };
    use windows::Win32::UI::Shell::{
        INotificationActivationCallback, INotificationActivationCallback_Impl,
        NOTIFICATION_USER_INPUT_DATA,
    };

    /// Хэндл приложения для колбэка: Windows зовёт `Activate` из своего потока.
    static APP: OnceLock<AppHandle> = OnceLock::new();
    /// Cookie от `CoRegisterClassObject`; 0 — не зарегистрированы.
    static COOKIE: AtomicU32 = AtomicU32::new(0);

    pub fn is_registered() -> bool {
        COOKIE.load(Ordering::Relaxed) != 0
    }

    pub fn init(app: &AppHandle) -> Result<(), String> {
        let _ = APP.set(app.clone());
        register_clsid_in_registry()?;
        register_class_object()?;
        super::log("COM-активатор тостов зарегистрирован");
        Ok(())
    }

    fn wide(value: &str) -> Vec<u16> {
        value.encode_utf16().chain(std::iter::once(0)).collect()
    }

    fn clsid() -> GUID {
        GUID::from_u128(super::TOAST_ACTIVATOR_CLSID_U128)
    }

    /// `HKCU\Software\Classes\CLSID\{CLSID}\LocalServer32` = путь к exe.
    /// HKCU — прав администратора не требует, установка per-user (04 §1.3).
    fn register_clsid_in_registry() -> Result<(), String> {
        let exe = std::env::current_exe()
            .map_err(|e| format!("current_exe: {e}"))?
            .to_string_lossy()
            .to_string();
        let subkey = wide(&format!(
            "Software\\Classes\\CLSID\\{}\\LocalServer32",
            super::TOAST_ACTIVATOR_CLSID
        ));
        let command = wide(&format!("\"{exe}\" --toast-activated"));

        unsafe {
            let mut key = HKEY::default();
            // CHECK: сигнатура RegCreateKeyExW в windows 0.58.
            let status = RegCreateKeyExW(
                HKEY_CURRENT_USER,
                PCWSTR(subkey.as_ptr()),
                Some(0),
                PCWSTR::null(),
                REG_OPTION_NON_VOLATILE,
                KEY_WRITE,
                None,
                &mut key,
                None,
            );
            if status.is_err() {
                return Err(format!("RegCreateKeyExW: {status:?}"));
            }

            let bytes = std::slice::from_raw_parts(
                command.as_ptr() as *const u8,
                std::mem::size_of_val(&command[..]),
            );
            let status = RegSetValueExW(key, PCWSTR::null(), Some(0), REG_SZ, Some(bytes));
            let _ = RegCloseKey(key);
            if status.is_err() {
                return Err(format!("RegSetValueExW: {status:?}"));
            }
        }
        Ok(())
    }

    fn register_class_object() -> Result<(), String> {
        // CHECK (windows 0.61): `#[implement]` генерирует `From<Factory>` для
        // каждого реализованного интерфейса. Если версия крейта поднимется и
        // `.into()` отвалится — эквивалент: `ComObject::new(Factory).to_interface()`.
        let factory: IClassFactory = Factory.into();
        let unknown: IUnknown = factory.cast().map_err(|e| format!("cast IUnknown: {e}"))?;
        unsafe {
            // CoInitializeEx для главного потока уже сделан рантаймом Tauri.
            let cookie =
                CoRegisterClassObject(&clsid(), &unknown, CLSCTX_LOCAL_SERVER, REGCLS_MULTIPLEUSE)
                    .map_err(|e| format!("CoRegisterClassObject: {e}"))?;
            COOKIE.store(cookie, Ordering::Relaxed);
        }
        Ok(())
    }

    /// Фабрика класса: Windows просит у нас экземпляр активатора.
    #[implement(IClassFactory)]
    struct Factory;

    // CHECK (windows 0.61): начиная с 0.59 трейты `*_Impl` реализуются на
    // сгенерированном типе `Factory_Impl`, а не на `Factory` (в 0.58 было наоборот).
    impl IClassFactory_Impl for Factory_Impl {
        fn CreateInstance(
            &self,
            outer: Option<&IUnknown>,
            iid: *const GUID,
            object: *mut *mut c_void,
        ) -> WinResult<()> {
            if object.is_null() || iid.is_null() {
                return Err(E_POINTER.into());
            }
            unsafe { *object = std::ptr::null_mut() };
            if outer.is_some() {
                // Агрегация не поддерживается.
                return Err(E_INVALIDARG.into());
            }
            let activator: INotificationActivationCallback = Activator.into();
            unsafe { activator.query(iid, object).ok() }
        }

        fn LockServer(&self, _lock: BOOL) -> WinResult<()> {
            Ok(())
        }
    }

    /// Собственно активатор.
    #[implement(INotificationActivationCallback)]
    struct Activator;

    // CHECK (windows 0.61): трейт реализуется на `Activator_Impl` — см. заметку у Factory.
    impl INotificationActivationCallback_Impl for Activator_Impl {
        fn Activate(
            &self,
            _app_user_model_id: &PCWSTR,
            invoked_args: &PCWSTR,
            data: *const NOTIFICATION_USER_INPUT_DATA,
            count: u32,
        ) -> WinResult<()> {
            let args = unsafe { invoked_args.to_string() }.unwrap_or_default();

            // Ищем поле ввода replyText среди пар «ключ → значение».
            let mut reply: Option<String> = None;
            if !data.is_null() {
                let items = unsafe { std::slice::from_raw_parts(data, count as usize) };
                for item in items {
                    let key = unsafe { item.Key.to_string() }.unwrap_or_default();
                    if key == "replyText" {
                        reply = unsafe { item.Value.to_string() }.ok();
                        break;
                    }
                }
            }

            match APP.get() {
                Some(app) => {
                    super::log(&format!("COM-активация: args={args}"));
                    crate::notify::on_toast_activated(app, &args, reply.as_deref());
                }
                None => super::log("COM-активация до инициализации приложения — игнорируем"),
            }
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn guid_string(value: u128) -> String {
        format!(
            "{{{:08X}-{:04X}-{:04X}-{:04X}-{:012X}}}",
            (value >> 96) as u32,
            ((value >> 80) & 0xFFFF) as u16,
            ((value >> 64) & 0xFFFF) as u16,
            ((value >> 48) & 0xFFFF) as u16,
            (value & 0xFFFF_FFFF_FFFF) as u64,
        )
    }

    #[test]
    fn default_build_uses_protocol_fallback() {
        // На macOS и на Windows без фичи toast-com-activator — фолбэк 04 §4.2.
        assert_eq!(mode(), ActivationMode::Protocol);
        assert!(!mode().is_com_callback());
    }

    #[test]
    fn clsid_string_and_number_match() {
        assert_eq!(
            guid_string(TOAST_ACTIVATOR_CLSID_U128),
            TOAST_ACTIVATOR_CLSID
        );
        assert_eq!(TOAST_ACTIVATOR_CLSID.len(), 38, "формат {{8-4-4-4-12}}");
        assert_eq!(TOAST_ACTIVATOR_CLSID.matches('-').count(), 4);
    }
}
