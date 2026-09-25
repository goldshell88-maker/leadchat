//! Защита мастер-ключа кэша на диске (04-DESKTOP §5.5).
//!
//! ```text
//! random 32 байта (мастер-ключ, генерируется при первом запуске)
//!         │ CryptProtectData (DPAPI, scope: current user, entropy: константа приложения)
//!         ▼
//! %APPDATA%\ru.partner-lead-centre.leadchat\cache.key   (DPAPI-блоб на диске)
//! ```
//!
//! * **Windows (`cfg(windows)`)** — настоящий DPAPI: `CryptProtectData` /
//!   `CryptUnprotectData` с привязкой к профилю текущего пользователя и энтропией
//!   приложения. Файл `cache.key`, скопированный к другому пользователю или на другую
//!   машину, не разворачивается → `cache.db` бесполезен (пункт приёмки 04 §10).
//! * **не-Windows (`cfg(not(windows))`)** — ЗАГЛУШКА ДЛЯ РАЗРАБОТКИ на macOS/Linux.
//!   Никакой защиты: ключ лежит в файле с правами 0600 и в лог печатается явный warning.
//!   Продуктовая сборка — только `windows-latest` в CI (04 §7).

use std::path::Path;

/// Энтропия DPAPI: связывает блоб именно с нашим приложением и версией схемы.
#[allow(dead_code)]
const ENTROPY: &[u8] = b"leadchat.cache.v1";

// ---------------------------------------------------------------------------
// Windows: настоящий DPAPI
// ---------------------------------------------------------------------------
#[cfg(target_os = "windows")]
mod imp {
    use super::ENTROPY;
    use windows::Win32::Foundation::{LocalFree, HLOCAL};
    use windows::Win32::Security::Cryptography::{
        CryptProtectData, CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
    };

    fn blob(data: &[u8]) -> CRYPT_INTEGER_BLOB {
        CRYPT_INTEGER_BLOB {
            cbData: data.len() as u32,
            pbData: data.as_ptr() as *mut u8,
        }
    }

    /// Забрать результат из выходного блоба и освободить память, выделенную DPAPI.
    unsafe fn take(out: &CRYPT_INTEGER_BLOB) -> Vec<u8> {
        let v = unsafe { std::slice::from_raw_parts(out.pbData, out.cbData as usize) }.to_vec();
        let _ = unsafe { LocalFree(Some(HLOCAL(out.pbData as _))) };
        v
    }

    pub fn protect(plain: &[u8]) -> Result<Vec<u8>, String> {
        let mut out = CRYPT_INTEGER_BLOB::default();
        unsafe {
            // CHECK: точные сигнатуры сверить с версией crate `windows` на момент сборки
            // (док 04 §5.5 писан под windows 0.58).
            CryptProtectData(
                &blob(plain),
                windows::core::PCWSTR::null(),
                Some(&blob(ENTROPY)),
                None,
                None,
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut out,
            )
            .map_err(|e| format!("CryptProtectData: {e}"))?;
            Ok(take(&out))
        }
    }

    pub fn unprotect(cipher: &[u8]) -> Result<Vec<u8>, String> {
        let mut out = CRYPT_INTEGER_BLOB::default();
        unsafe {
            CryptUnprotectData(
                &blob(cipher),
                None,
                Some(&blob(ENTROPY)),
                None,
                None,
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut out,
            )
            .map_err(|e| format!("CryptUnprotectData: {e}"))?;
            Ok(take(&out))
        }
    }

    /// На Windows права файла не трогаем: защищает сам DPAPI-блоб, а не ACL.
    pub fn harden_file(_path: &std::path::Path) {}
}

// ---------------------------------------------------------------------------
// не-Windows: dev-заглушка (НЕ защита)
// ---------------------------------------------------------------------------
#[cfg(not(target_os = "windows"))]
mod imp {
    use std::path::Path;
    use std::sync::Once;

    /// Маркер, чтобы dev-блоб нельзя было спутать с настоящим DPAPI-блобом
    /// (и чтобы Windows-сборка честно упала на нём, а не «расшифровала мусор»).
    const DEV_MAGIC: &[u8; 8] = b"LCDEVKEY";

    static WARNED: Once = Once::new();

    fn warn_once() {
        WARNED.call_once(|| {
            eprintln!(
                "[leadchat][cache] WARNING: DPAPI недоступен на этой платформе — \
                 мастер-ключ кэша хранится в файле БЕЗ ШИФРОВАНИЯ (только права 0600). \
                 Это заглушка ДЛЯ РАЗРАБОТКИ; продуктовая сборка — только Windows (04 §7). \
                 Не держите в этом кэше реальные данные клиентов."
            );
        });
    }

    pub fn protect(plain: &[u8]) -> Result<Vec<u8>, String> {
        warn_once();
        let mut out = Vec::with_capacity(DEV_MAGIC.len() + plain.len());
        out.extend_from_slice(DEV_MAGIC);
        out.extend_from_slice(plain);
        Ok(out)
    }

    pub fn unprotect(cipher: &[u8]) -> Result<Vec<u8>, String> {
        warn_once();
        if cipher.len() <= DEV_MAGIC.len() || &cipher[..DEV_MAGIC.len()] != DEV_MAGIC {
            return Err("cache.key: не dev-формат (создан на другой платформе)".to_string());
        }
        Ok(cipher[DEV_MAGIC.len()..].to_vec())
    }

    /// Права 0600 — единственная (слабая) защита в dev-режиме.
    pub fn harden_file(path: &Path) {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if let Err(e) = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)) {
                eprintln!("[leadchat][cache] не удалось выставить 0600 на {path:?}: {e}");
            }
        }
        #[cfg(not(unix))]
        let _ = path;
    }
}

/// Зашифровать блоб «ключом пользователя ОС».
pub fn protect(plain: &[u8]) -> Result<Vec<u8>, String> {
    imp::protect(plain)
}

/// Обратная операция к [`protect`].
pub fn unprotect(cipher: &[u8]) -> Result<Vec<u8>, String> {
    imp::unprotect(cipher)
}

/// Записать защищённый блоб в файл (и ужесточить права там, где это единственная защита).
pub fn write_protected(path: &Path, plain: &[u8]) -> Result<(), String> {
    let wrapped = protect(plain)?;
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| format!("{path:?}: {e}"))?;
    }
    std::fs::write(path, &wrapped).map_err(|e| format!("{path:?}: {e}"))?;
    imp::harden_file(path);
    Ok(())
}

/// Прочитать и развернуть защищённый блоб из файла.
pub fn read_protected(path: &Path) -> Result<Vec<u8>, String> {
    let wrapped = std::fs::read(path).map_err(|e| format!("{path:?}: {e}"))?;
    unprotect(&wrapped)
}

// ---------------------------------------------------------------------------
// Команды для служебных данных кэша (04 §8.1)
// ---------------------------------------------------------------------------

/// DPAPI-шифрование произвольного блоба (служебные данные кэша).
///
/// Мастер-ключ кэша через эти команды НЕ проходит — он не покидает Rust.
#[tauri::command]
pub fn dpapi_encrypt(data: Vec<u8>) -> Result<Vec<u8>, String> {
    protect(&data)
}

/// Обратная операция к [`dpapi_encrypt`].
#[tauri::command]
pub fn dpapi_decrypt(data: Vec<u8>) -> Result<Vec<u8>, String> {
    unprotect(&data)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip() {
        let key = [42u8; 32];
        let wrapped = protect(&key).unwrap();
        assert_ne!(wrapped.as_slice(), key.as_slice(), "блоб != открытый ключ");
        assert_eq!(unprotect(&wrapped).unwrap(), key.to_vec());
    }

    #[test]
    fn garbage_is_rejected() {
        assert!(unprotect(b"not-a-blob").is_err());
    }

    #[test]
    fn file_roundtrip() {
        let dir = std::env::temp_dir().join(format!("lc-dpapi-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("cache.key");
        write_protected(&path, &[1u8; 32]).unwrap();
        assert_eq!(read_protected(&path).unwrap(), vec![1u8; 32]);
        #[cfg(all(unix, not(target_os = "windows")))]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&path).unwrap().permissions().mode();
            assert_eq!(mode & 0o777, 0o600, "dev-ключ обязан быть 0600");
        }
        std::fs::remove_dir_all(&dir).ok();
    }
}
