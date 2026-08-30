//! The macOS shell: an NSPanel on a global hotkey.
//!
//! A plain Tauri window would look right and behave wrong. It cannot appear
//! over a fullscreen app, it activates Muster and pushes whatever you were
//! reading behind it, and it puts an icon in the Dock for something you meant
//! to summon for four seconds. `NSPanel` with a non-activating style mask is
//! what Spotlight and Raycast actually are, and `tauri-nspanel` is the only
//! way to reach it from Tauri.
//!
//! Nothing here knows anything about retrieval. The panel talks to
//! `muster serve` over loopback HTTP, which the user starts themselves.

use tauri::{Manager, WebviewUrl};

#[cfg(target_os = "macos")]
use tauri_nspanel::{
    tauri_panel, CollectionBehavior, ManagerExt, PanelBuilder, PanelLevel, StyleMask,
};

pub const PANEL: &str = "spotlight";

#[cfg(target_os = "macos")]
tauri_panel! {
    panel!(SpotlightPanel {
        config: {
            // The query field needs the keyboard, so the panel must be able to
            // become key — but the *style mask* stays non-activating, so
            // taking the keyboard never activates the application or disturbs
            // the window behind it.
            can_become_key_window: true,
            can_become_main_window: false,
            is_floating_panel: true,
            // We hide on resign-key instead. hides_on_deactivate would also
            // fire when the app loses focus for reasons that are not the user
            // dismissing the panel.
            hides_on_deactivate: false
        }
    })

    panel_event!(SpotlightEvents {
        window_did_resign_key(notification: &NSNotification) -> ()
    })
}

#[tauri::command]
fn hide_panel(app: tauri::AppHandle) {
    hide(&app);
}

#[cfg(target_os = "macos")]
fn hide(app: &tauri::AppHandle) {
    if let Ok(panel) = app.get_webview_panel(PANEL) {
        panel.hide();
    }
}

#[cfg(not(target_os = "macos"))]
fn hide(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window(PANEL) {
        let _ = window.hide();
    }
}

#[cfg(target_os = "macos")]
fn toggle(app: &tauri::AppHandle) {
    let Ok(panel) = app.get_webview_panel(PANEL) else {
        return;
    };
    if panel.is_visible() {
        panel.hide();
    } else {
        // Centre on the screen the pointer is on, not the one the panel was
        // last shown on: a summoned panel should appear where you are looking.
        if let Some(window) = panel.to_window() {
            let _ = window.center();
        }
        panel.show_and_make_key();
    }
}

#[cfg(not(target_os = "macos"))]
fn toggle(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window(PANEL) {
        if window.is_visible().unwrap_or(false) {
            let _ = window.hide();
        } else {
            let _ = window.show();
            let _ = window.set_focus();
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let mut builder = tauri::Builder::default().plugin(tauri_plugin_opener::init());

    #[cfg(target_os = "macos")]
    {
        builder = builder.plugin(tauri_nspanel::init());
    }

    builder
        .invoke_handler(tauri::generate_handler![hide_panel])
        .setup(|app| {
            // No Dock icon and no menu bar: this is summoned, not launched.
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            build_panel(app.handle())?;
            register_hotkey(app.handle())?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running muster");
}

#[cfg(target_os = "macos")]
fn build_panel(app: &tauri::AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    use tauri::{LogicalSize, Size};

    let panel = PanelBuilder::<_, SpotlightPanel>::new(app, PANEL)
        .url(WebviewUrl::App("index.html".into()))
        .title("Muster")
        .size(Size::Logical(LogicalSize::new(720.0, 460.0)))
        .transparent(true)
        .has_shadow(true)
        .corner_radius(12.0)
        // Above normal windows but below the menu bar, which is where a
        // summoned launcher belongs.
        .level(PanelLevel::Floating)
        .floating(true)
        .hides_on_deactivate(false)
        .style_mask(StyleMask::new().nonactivating_panel())
        .collection_behavior(
            CollectionBehavior::new()
                // The two that matter: appear on whichever Space is in front,
                // including over an app that is fullscreen.
                .can_join_all_spaces()
                .full_screen_auxiliary()
                // Not a window you Cmd-Tab to.
                .ignores_cycle(),
        )
        .with_window(|window| window.decorations(false).resizable(false).visible(false))
        .build()?;

    // Dismiss on blur. Clicking away is the ordinary way to close a launcher,
    // and leaving it up over another app is worse than closing it too eagerly.
    let handler = SpotlightEvents::new();
    let handle = app.clone();
    handler.window_did_resign_key(move |_notification| {
        hide(&handle);
    });
    panel.set_event_handler(Some(handler.as_ref()));

    Ok(())
}

#[cfg(not(target_os = "macos"))]
fn build_panel(app: &tauri::AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    // Everything the panel gives us is macOS-only. Elsewhere this is an
    // ordinary window, which is honest about what it is rather than a
    // half-imitation.
    tauri::WebviewWindowBuilder::new(app, PANEL, WebviewUrl::App("index.html".into()))
        .title("Muster")
        .inner_size(720.0, 460.0)
        .decorations(false)
        .always_on_top(true)
        .visible(false)
        .build()?;
    Ok(())
}

fn register_hotkey(app: &tauri::AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    use tauri_plugin_global_shortcut::{
        Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState,
    };

    // Cmd+Shift+Space. Cmd+Space alone belongs to Spotlight, and taking it
    // from a user who did not ask is not ours to do.
    let hotkey = Shortcut::new(Some(Modifiers::SUPER | Modifiers::SHIFT), Code::Space);

    app.plugin(
        tauri_plugin_global_shortcut::Builder::new()
            .with_handler(move |app, shortcut, event| {
                if event.state() == ShortcutState::Pressed && shortcut == &hotkey {
                    toggle(app);
                }
            })
            .build(),
    )?;

    app.global_shortcut().register(hotkey)?;
    Ok(())
}
