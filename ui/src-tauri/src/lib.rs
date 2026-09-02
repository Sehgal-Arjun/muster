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

use tauri::{Emitter, Manager};

#[cfg(target_os = "macos")]
use tauri_nspanel::{
    tauri_panel, CollectionBehavior, ManagerExt, PanelLevel, StyleMask, WebviewWindowExt,
};

pub const PANEL: &str = "spotlight";
/// Emitted on every summon so the frontend can take focus. See `toggle`.
pub const SHOWN_EVENT: &str = "muster://shown";

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
    on_main(app, |panel| panel.hide());
}

/// Run something against the panel on the main thread.
///
/// Everything here is AppKit, and AppKit off the main thread does not raise —
/// it quietly does nothing. Both dismissal paths arrive off it: a `#[tauri::command]`
/// is dispatched to a worker thread, where the panel store is not even reachable,
/// and the global-shortcut handler is called from the shortcut plugin's own
/// thread, where `hide` returned without hiding anything. That is the whole of
/// "escape does nothing" and "the hotkey will not close it": the calls were
/// being made, on a thread where they could not take effect.
#[cfg(target_os = "macos")]
fn on_main<F>(app: &tauri::AppHandle, work: F)
where
    F: FnOnce(tauri_nspanel::PanelHandle<tauri::Wry>) + Send + 'static,
{
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        if let Ok(panel) = handle.get_webview_panel(PANEL) {
            work(panel);
        }
    });
}

#[cfg(not(target_os = "macos"))]
fn hide(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window(PANEL) {
        let _ = window.hide();
    }
}

#[cfg(target_os = "macos")]
fn toggle(app: &tauri::AppHandle) {
    let handle = app.clone();
    on_main(app, move |panel| {
        if panel.is_visible() {
            panel.hide();
        } else {
            // Centre before showing, so a summon never lands somewhere stale.
            //
            // Look the window up rather than calling `panel.to_window()`. That
            // is a conversion, not an accessor: it unregisters the panel, drops
            // the resign-key handler, and sets the Objective-C class back to
            // NSWindow. Reaching through it for `center` destroyed the panel on
            // the very first summon — which is why Escape then found nothing to
            // hide, the hotkey could not toggle, and clicking away never
            // dismissed. All three were the same line.
            if let Some(window) = handle.get_webview_window(PANEL) {
                let _ = window.center();
            }
            panel.show_and_make_key();
            // Not redundant with show_and_make_key. Under ActivationPolicy::Accessory
            // the app is never frontmost, and ordering the panel front does not by
            // itself hand it key status — without this the panel renders but no
            // keystroke reaches the webview, and window_did_resign_key can never
            // fire because the panel was never key, so click-away never dismisses.
            panel.make_key_window();
            // The webview is not remounted between summons, so the text field's
            // focus-on-mount only ever fires once. Every subsequent summon needs to
            // be told to take focus again, or the panel appears and swallows typing.
            let _ = handle.emit(SHOWN_EVENT, ());
        }
    });
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
    let mut builder = tauri::Builder::default()
        // Must be registered first, per the plugin's contract.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            // A second launch summons the copy already running rather than
            // starting a rival that fights it for the hotkey.
            toggle(app);
        }))
        .plugin(tauri_plugin_opener::init());

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
    // The window is declared undecorated in tauri.conf.json and converted here,
    // rather than built by PanelBuilder.
    //
    // Both alternatives abort the process, and it took a while to see why. tao
    // wires up the titlebar and its traffic-light buttons when it creates a
    // window; clearing the Titled bit from the style mask afterwards leaves it
    // holding views that no longer exist, and the resulting panic crosses an
    // Objective-C frame, so it cannot unwind and takes the process with it.
    // StyleMask::new() starts from Titled|Closable|Miniaturizable|Resizable, so
    // the only safe way to a bare panel is never to ask for decorations at all.
    let window = app
        .get_webview_window(PANEL)
        .ok_or("window `spotlight` is missing from tauri.conf.json")?;

    // Frosted background. Applied to the window before conversion, while it is
    // still an ordinary NSWindow.
    #[cfg(target_os = "macos")]
    window_vibrancy::apply_vibrancy(
        &window,
        window_vibrancy::NSVisualEffectMaterial::HudWindow,
        Some(window_vibrancy::NSVisualEffectState::Active),
        Some(12.0),
    )?;

    let panel = window.to_panel::<SpotlightPanel>()?;

    panel.set_level(PanelLevel::Floating.into());
    panel.set_floating_panel(true);
    panel.set_hides_on_deactivate(false);
    // Safe here, unlike on a decorated window: there is no titlebar to orphan.
    panel.set_style_mask(StyleMask::empty().nonactivating_panel().into());
    panel.set_collection_behavior(
        CollectionBehavior::new()
            // The two that matter: appear on whichever Space is in front,
            // including over an app that is fullscreen.
            .can_join_all_spaces()
            .full_screen_auxiliary()
            // Not a window you Cmd-Tab to.
            .ignores_cycle()
            .into(),
    );

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
    tauri::WebviewWindowBuilder::new(app, PANEL, tauri::WebviewUrl::App("index.html".into()))
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

    // Propagated, not logged: if the shortcut is taken the app has no other way
    // in at all, and a line on stderr nobody reads is indistinguishable from it
    // working.
    app.global_shortcut().register(hotkey)?;
    Ok(())
}
