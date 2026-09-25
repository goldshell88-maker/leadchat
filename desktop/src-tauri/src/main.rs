// Windows: в релизе не показываем консольное окно (в debug оно нужно для логов).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    leadchat_desktop::run()
}
