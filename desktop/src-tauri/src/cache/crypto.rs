//! AES-256-GCM поверх мастер-ключа кэша (04-DESKTOP §5.5).
//!
//! Формат значения в колонке `*_enc`: `nonce(12) || ciphertext||tag`.
//! Nonce случайный на КАЖДУЮ запись (переиспользование nonce в GCM ломает схему).
//!
//! Мастер-ключ живёт только в памяти Rust-процесса: он приходит из
//! [`crate::cache::dpapi`] (на Windows — распакованный DPAPI-блоб) и в JS
//! не отдаётся никогда — фронт видит только расшифрованные значения
//! конкретных полей через `#[tauri::command]`.

use aes_gcm::aead::{Aead, KeyInit};
use aes_gcm::{Aes256Gcm, Key, Nonce};
use rand::RngCore;

/// Длина nonce AES-GCM.
pub const NONCE_LEN: usize = 12;
/// Длина мастер-ключа.
pub const KEY_LEN: usize = 32;

/// Обёртка над шифром с мастер-ключом. `Send + Sync`, живёт в `CacheState`.
pub struct Crypto {
    cipher: Aes256Gcm,
}

impl std::fmt::Debug for Crypto {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Никогда не печатаем ключ, даже в Debug.
        f.write_str("Crypto { master_key: <redacted> }")
    }
}

impl Crypto {
    /// Создать шифр из 32-байтового мастер-ключа.
    pub fn new(master: &[u8; KEY_LEN]) -> Self {
        let key = Key::<Aes256Gcm>::from_slice(master);
        Self {
            cipher: Aes256Gcm::new(key),
        }
    }

    /// Зашифровать произвольные байты → `nonce || ciphertext`.
    pub fn encrypt_field(&self, plain: &[u8]) -> Result<Vec<u8>, String> {
        let mut nonce_bytes = [0u8; NONCE_LEN];
        rand::rngs::OsRng.fill_bytes(&mut nonce_bytes);
        let nonce = Nonce::from_slice(&nonce_bytes);
        let ct = self
            .cipher
            .encrypt(nonce, plain)
            .map_err(|_| "cache: encrypt failed".to_string())?;
        let mut out = Vec::with_capacity(NONCE_LEN + ct.len());
        out.extend_from_slice(&nonce_bytes);
        out.extend_from_slice(&ct);
        Ok(out)
    }

    /// Расшифровать `nonce || ciphertext`.
    pub fn decrypt_field(&self, blob: &[u8]) -> Result<Vec<u8>, String> {
        if blob.len() <= NONCE_LEN {
            return Err("cache: ciphertext too short".to_string());
        }
        let (nonce_bytes, ct) = blob.split_at(NONCE_LEN);
        let nonce = Nonce::from_slice(nonce_bytes);
        self.cipher
            .decrypt(nonce, ct)
            .map_err(|_| "cache: decrypt failed (wrong key or corrupted row)".to_string())
    }

    /// Зашифровать строку.
    pub fn encrypt_str(&self, plain: &str) -> Result<Vec<u8>, String> {
        self.encrypt_field(plain.as_bytes())
    }

    /// Расшифровать строку.
    pub fn decrypt_str(&self, blob: &[u8]) -> Result<String, String> {
        let bytes = self.decrypt_field(blob)?;
        String::from_utf8(bytes).map_err(|_| "cache: value is not valid utf-8".to_string())
    }

    /// `Option<&str>` → `Option<BLOB>` (None остаётся NULL в SQLite).
    pub fn encrypt_opt(&self, plain: Option<&str>) -> Result<Option<Vec<u8>>, String> {
        match plain {
            Some(v) => Ok(Some(self.encrypt_str(v)?)),
            None => Ok(None),
        }
    }

    /// `Option<BLOB>` → `Option<String>`.
    ///
    /// Битую/чужую строку не считаем фатальной: кэш — всего лишь проекция сервера,
    /// поэтому нерасшифровываемое значение отдаётся как `None`, а не как ошибка
    /// всей выборки (иначе одна повреждённая строка гасила бы весь список).
    pub fn decrypt_opt(&self, blob: Option<Vec<u8>>) -> Option<String> {
        blob.and_then(|b| self.decrypt_str(&b).ok())
    }

    /// JSON-значение → BLOB.
    pub fn encrypt_json(&self, value: &serde_json::Value) -> Result<Vec<u8>, String> {
        let s = serde_json::to_string(value).map_err(|e| e.to_string())?;
        self.encrypt_str(&s)
    }

    /// BLOB → JSON-значение (битое значение → `None`, см. [`Self::decrypt_opt`]).
    pub fn decrypt_json(&self, blob: Option<Vec<u8>>) -> Option<serde_json::Value> {
        let s = self.decrypt_opt(blob)?;
        serde_json::from_str(&s).ok()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn crypto() -> Crypto {
        Crypto::new(&[7u8; KEY_LEN])
    }

    #[test]
    fn roundtrip_str() {
        let c = crypto();
        let blob = c.encrypt_str("Иван Петров · +79261234567").unwrap();
        assert_eq!(c.decrypt_str(&blob).unwrap(), "Иван Петров · +79261234567");
    }

    #[test]
    fn nonce_is_per_record() {
        let c = crypto();
        let a = c.encrypt_str("одно и то же").unwrap();
        let b = c.encrypt_str("одно и то же").unwrap();
        assert_ne!(a, b, "nonce обязан быть свой на каждую запись");
        assert_eq!(c.decrypt_str(&a).unwrap(), c.decrypt_str(&b).unwrap());
    }

    #[test]
    fn foreign_key_cannot_decrypt() {
        let blob = crypto().encrypt_str("секрет").unwrap();
        let other = Crypto::new(&[9u8; KEY_LEN]);
        assert!(other.decrypt_str(&blob).is_err());
    }

    #[test]
    fn tampered_ciphertext_is_rejected() {
        let c = crypto();
        let mut blob = c.encrypt_str("секрет").unwrap();
        let last = blob.len() - 1;
        blob[last] ^= 0xff;
        assert!(c.decrypt_field(&blob).is_err());
    }

    #[test]
    fn short_blob_is_rejected() {
        assert!(crypto().decrypt_field(&[0u8; 4]).is_err());
    }
}
