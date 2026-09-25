fn main() {
    // Генерирует контекст Tauri (парсит tauri.conf.json, схемы capabilities,
    // на Windows — ресурсы exe: иконка, манифест). На macOS/Linux шаг Windows-ресурсов
    // пропускается сам, поэтому `cargo check` проходит везде (04 §1, ограничение среды).
    tauri_build::build()
}
