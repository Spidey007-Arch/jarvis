mod supervisor;

use std::sync::Arc;
use supervisor::{ConnInfo, Status, Supervisor};
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, RunEvent, WindowEvent};

/// Until the Settings screen exists, closing the window quits the app (and
/// stops the backend and agent). The tray menu still offers Show / Quit.
const CLOSE_TO_TRAY: bool = false;

#[tauri::command]
fn get_status(sup: tauri::State<Arc<Supervisor>>) -> Status {
    sup.status()
}

#[tauri::command]
fn get_connection(sup: tauri::State<Arc<Supervisor>>) -> Option<ConnInfo> {
    sup.connection()
}

#[tauri::command]
fn restart_services(sup: tauri::State<Arc<Supervisor>>) {
    sup.request_restart();
}

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

pub fn run() {
    let sup = Arc::new(Supervisor::new());
    let sup_for_setup = sup.clone();
    let sup_for_exit = sup.clone();

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main(app);
        }))
        .manage(sup)
        .invoke_handler(tauri::generate_handler![get_status, get_connection, restart_services])
        .setup(move |app| {
            let show = MenuItem::with_id(app, "show", "Show JARVIS", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit JARVIS", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;
            let mut tray = TrayIconBuilder::new()
                .tooltip("JARVIS")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => show_main(app),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main(tray.app_handle());
                    }
                });
            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            tray.build(app)?;

            sup_for_setup.start(app.handle().clone());
            Ok(())
        })
        .on_window_event(|window, event| {
            if CLOSE_TO_TRAY {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("failed to build the JARVIS application")
        .run(move |_app, event| {
            if let RunEvent::Exit = event {
                sup_for_exit.shutdown();
            }
        });
}
