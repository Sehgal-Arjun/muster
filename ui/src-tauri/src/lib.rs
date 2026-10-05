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

use std::sync::atomic::{AtomicBool, Ordering};

use tauri::{Emitter, Manager};

/// Whether the reader has picked a place for the panel.
///
/// Until they do, every summon centres it, which is what a launcher should do.
/// Once they have dragged it somewhere, centring would throw their choice away —
/// so from then on the panel appears where they left it, and a layout change
/// keeps the middle of the window where the middle of the window was.
static PLACED: AtomicBool = AtomicBool::new(false);

/// Where the panel was left, across launches.
///
/// A window you positioned and then found back in the middle of the screen after
/// a restart has not really been positioned. One file, two numbers, written when
/// the panel hides — which is the moment the position stops changing.
#[cfg(target_os = "macos")]
fn placement_path() -> Option<std::path::PathBuf> {
    std::env::var_os("HOME").map(|home| {
        std::path::Path::new(&home).join(".muster").join("panel.json")
    })
}

#[cfg(target_os = "macos")]
fn remember_placement(app: &tauri::AppHandle) {
    if !PLACED.load(Ordering::Relaxed) {
        return;    // never moved: centring on the next summon is still right
    }
    let (Some(path), Some(window)) = (placement_path(), app.get_webview_window(PANEL))
    else {
        return;
    };
    let Ok(position) = window.outer_position() else { return };
    let scale = window.scale_factor().unwrap_or(1.0);
    let at = position.to_logical::<f64>(scale);
    let _ = std::fs::write(path, format!("{{\"x\": {}, \"y\": {}}}", at.x, at.y));
}

#[cfg(target_os = "macos")]
fn restore_placement(app: &tauri::AppHandle) {
    let (Some(path), Some(window)) = (placement_path(), app.get_webview_window(PANEL))
    else {
        return;
    };
    let Ok(text) = std::fs::read_to_string(path) else { return };
    if let Some((x, y)) = parse_placement(&text) {
        PLACED.store(true, Ordering::Relaxed);
        let _ = window.set_position(tauri::LogicalPosition::new(x, y));
    }
}
/// The two numbers out of the placement file.
///
/// Hand-rolled rather than reaching for a JSON crate to read two floats — and
/// pure, because hand-rolled parsing is exactly the sort of thing that fails
/// silently and leaves a window quietly refusing to go where it was put.
fn parse_placement(text: &str) -> Option<(f64, f64)> {
    fn number(text: &str, key: &str) -> Option<f64> {
        let start = text.find(key)? + key.len();
        let rest = text[start..].trim_start();
        let rest = rest.strip_prefix(':')?.trim_start();
        let end = rest
            .find(|c: char| !c.is_ascii_digit() && c != '-' && c != '.')
            .unwrap_or(rest.len());
        rest[..end].parse().ok()
    }
    Some((number(text, "\"x\"")?, number(text, "\"y\"")?))
}


#[cfg(target_os = "macos")]
use tauri_nspanel::{
    tauri_panel, CollectionBehavior, ManagerExt, PanelLevel, StyleMask, WebviewWindowExt,
};

pub const PANEL: &str = "spotlight";
/// The launchd job that runs the read-only API the panel talks to. Kept in step
/// with packaging/install.sh by hand; there is only one of each.
#[cfg(target_os = "macos")]
const SERVE_LABEL: &str = "dev.muster.desktop.serve";

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

/// Ask launchd to start the API server, for when the panel finds it absent.
///
/// The panel cannot do its job without it, and "run `muster serve`" is a poor
/// answer to give someone who has just pressed a hotkey. launchd already owns
/// the process — this only tells it to get on with it, so the server stays a
/// separate thing that the panel merely talks to rather than something bundled
/// inside the app.
#[cfg(target_os = "macos")]
#[tauri::command]
fn start_server() -> Result<String, String> {
    // The uid from the home directory's owner, rather than a dependency for one
    // number: launchd domains are per user and this is the user's own home.
    use std::os::unix::fs::MetadataExt;

    let home = std::env::var("HOME").map_err(|_| "HOME is not set".to_string())?;
    let uid = std::fs::metadata(&home)
        .map_err(|e| format!("cannot stat {home}: {e}"))?
        .uid();
    let domain = format!("gui/{uid}/{SERVE_LABEL}");
    let output = std::process::Command::new("launchctl")
        .args(["kickstart", "-k", &domain])
        .output()
        .map_err(|e| format!("could not run launchctl: {e}"))?;

    if output.status.success() {
        return Ok("starting".into());
    }
    // Not loaded at all: the installer has never run, or ran under an older
    // label. Saying so is more use than "failed".
    Err(format!(
        "launchd has no job {SERVE_LABEL} — run packaging/install.sh ({})",
        String::from_utf8_lossy(&output.stderr).trim()
    ))
}

#[cfg(not(target_os = "macos"))]
#[tauri::command]
fn start_server() -> Result<String, String> {
    Err("only macOS runs the server under launchd".into())
}

#[tauri::command]
fn hide_panel(app: tauri::AppHandle) {
    hide(&app);
}

/// Where a window of `size` sits, centred on a monitor of `screen` at `origin`.
///
/// Pure, so the arithmetic that was wrong can be tested. The bug was not the
/// formula — it was measuring the window instead of being told the size it is
/// about to become.
fn centred_origin(origin: (f64, f64), screen: (f64, f64), size: (f64, f64)) -> (f64, f64) {
    (
        origin.0 + (screen.0 - size.0) / 2.0,
        origin.1 + (screen.1 - size.1) / 2.0,
    )
}

/// Where a window of `size` sits so that its middle lands on `centre`.
///
/// What a resize does to a window the reader has placed themselves: it grows in
/// every direction rather than dragging the window off to one side.
fn anchored_origin(centre: (f64, f64), size: (f64, f64)) -> (f64, f64) {
    (centre.0 - size.0 / 2.0, centre.1 - size.1 / 2.0)
}

/// Centre the panel on the monitor it is on, for a known size.
///
/// Not `window.center()`. That measures the window as it is right now, and the
/// resize that is about to make it 1180 wide has not landed yet — so it centres
/// a 720-point window and the extra 460 grow off to the right. The panel opened
/// visibly right of centre, and the wider the layout the worse it got. Doing the
/// arithmetic against the size we are setting has no such ordering to get wrong.
#[cfg(target_os = "macos")]
fn centre_on_monitor(window: &tauri::WebviewWindow, size: tauri::LogicalSize<f64>) {
    if let Ok(Some(monitor)) = window.current_monitor() {
        let scale = monitor.scale_factor();
        let origin = monitor.position().to_logical::<f64>(scale);
        let screen = monitor.size().to_logical::<f64>(scale);
        let (x, y) = centred_origin(
            (origin.x, origin.y),
            (screen.width, screen.height),
            (size.width, size.height),
        );
        let _ = window.set_position(tauri::LogicalPosition::new(x, y));
    }
}

/// Begin an ordinary window drag, from a mousedown in the panel's chrome.
///
/// There is no titlebar to drag — that is the whole point of the panel — so the
/// query bar and the footer are the handles. AppKit runs the drag loop itself
/// once asked, which is why this is one call and not a stream of mouse events.
#[cfg(target_os = "macos")]
#[tauri::command]
fn start_drag(app: tauri::AppHandle) {
    // Recorded before the drag rather than after it: the reader has expressed a
    // preference by picking the window up, whether or not they move it far.
    PLACED.store(true, Ordering::Relaxed);
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        if let Some(window) = handle.get_webview_window(PANEL) {
            let _ = window.start_dragging();
        }
    });
}

#[cfg(not(target_os = "macos"))]
#[tauri::command]
fn start_drag(app: tauri::AppHandle) {
    if let Some(window) = app.get_webview_window(PANEL) {
        let _ = window.start_dragging();
    }
}

/// Resize the panel for the layout the reader has chosen.
///
/// A launcher and a reading window want different shapes: 720 points is right for
/// a list of results and far too narrow for an email beside one. Rather than
/// scale a single size, the panel becomes the size the chosen layout needs.
///
/// On the main thread, and through `get_webview_window` rather than
/// `panel.to_window()` — see `toggle` for what that conversion does.
#[cfg(target_os = "macos")]
#[tauri::command]
fn set_layout(app: tauri::AppHandle, wide: bool) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        if let Some(window) = handle.get_webview_window(PANEL) {
            let size = if wide {
                tauri::LogicalSize::new(1180.0, 700.0)
            } else {
                tauri::LogicalSize::new(720.0, 460.0)
            };

            // Where the middle of the window is now, before it changes size.
            let anchor = window.outer_position().ok().and_then(|position| {
                let scale = window.scale_factor().unwrap_or(1.0);
                let at = position.to_logical::<f64>(scale);
                window.inner_size().ok().map(|inner| {
                    let was = inner.to_logical::<f64>(scale);
                    (at.x + was.width / 2.0, at.y + was.height / 2.0)
                })
            });

            let _ = window.set_size(size);

            if PLACED.load(Ordering::Relaxed) {
                // Grow around the middle, so a window the reader placed stays
                // where they placed it rather than jumping back to centre.
                if let Some(centre) = anchor {
                    let (x, y) = anchored_origin(centre, (size.width, size.height));
                    let _ = window.set_position(tauri::LogicalPosition::new(x, y));
                }
            } else {
                centre_on_monitor(&window, size);
            }
        }
    });
}

#[cfg(not(target_os = "macos"))]
#[tauri::command]
fn set_layout(app: tauri::AppHandle, wide: bool) {
    if let Some(window) = app.get_webview_window(PANEL) {
        let _ = window.set_size(if wide {
            tauri::LogicalSize::new(1180.0, 700.0)
        } else {
            tauri::LogicalSize::new(720.0, 460.0)
        });
    }
}

#[cfg(target_os = "macos")]
fn hide(app: &tauri::AppHandle) {
    // Recorded on the way out: hiding is the moment the position stops changing,
    // and the panel is hidden far more often than the app is quit.
    remember_placement(app);
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
            // Only until the reader has picked a place for it. After that,
            // re-centring every summon would silently undo the drag.
            if !PLACED.load(Ordering::Relaxed) {
                if let Some(window) = handle.get_webview_window(PANEL) {
                    let scale = window.scale_factor().unwrap_or(1.0);
                    if let Ok(size) = window.inner_size() {
                        centre_on_monitor(&window, size.to_logical::<f64>(scale));
                    }
                }
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
        .invoke_handler(tauri::generate_handler![
            hide_panel, start_server, set_layout, start_drag
        ])
        .setup(|app| {
            // No Dock icon and no menu bar: this is summoned, not launched.
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            build_panel(app.handle())?;
            // Put it back where it was left, if it was ever moved.
            #[cfg(target_os = "macos")]
            restore_placement(app.handle());
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

#[cfg(test)]
mod tests {
    use super::{anchored_origin, centred_origin};

    /// The bug, written down: `window.center()` measured the window as it was —
    /// 720 wide — and the resize to 1180 that was already in flight grew off to
    /// the right, leaving the panel 230 points right of centre.
    #[test]
    fn centring_uses_the_size_being_set_not_the_size_it_had() {
        let screen = (1512.0, 982.0);
        let wide = centred_origin((0.0, 0.0), screen, (1180.0, 700.0));
        assert_eq!(wide.0, 166.0);
        // What the old code effectively did, and what it looked like.
        let stale = centred_origin((0.0, 0.0), screen, (720.0, 460.0));
        assert_eq!(stale.0, 396.0);
        assert_eq!(stale.0 - wide.0, 230.0);
    }

    #[test]
    fn centring_is_relative_to_the_monitor_the_panel_is_on() {
        // A second display to the right of the first.
        let on_second = centred_origin((1512.0, 0.0), (1920.0, 1080.0), (1180.0, 700.0));
        assert_eq!(on_second.0, 1512.0 + 370.0);
        assert_eq!(on_second.1, 190.0);
    }

    #[test]
    fn a_placement_file_round_trips() {
        assert_eq!(
            super::parse_placement("{\"x\": 412.5, \"y\": -18}"),
            Some((412.5, -18.0))
        );
        // A second display to the left gives negative coordinates, which is not
        // an error and must not be read as one.
        assert_eq!(super::parse_placement("{\"x\": -1512, \"y\": 0}"), Some((-1512.0, 0.0)));
    }

    #[test]
    fn a_damaged_placement_file_is_ignored_rather_than_guessed() {
        assert_eq!(super::parse_placement(""), None);
        assert_eq!(super::parse_placement("{\"x\": 10}"), None);
        assert_eq!(super::parse_placement("{\"x\": abc, \"y\": 3}"), None);
    }

    #[test]
    fn a_placed_window_grows_around_its_middle() {
        // Narrow panel dropped near the top left, then widened.
        let centre = (100.0 + 360.0, 80.0 + 230.0);
        let (x, y) = anchored_origin(centre, (1180.0, 700.0));
        assert_eq!(x, 460.0 - 590.0);
        assert_eq!(y, 310.0 - 350.0);
        // The middle is exactly where it was.
        assert_eq!(x + 590.0, centre.0);
        assert_eq!(y + 350.0, centre.1);
    }
}
