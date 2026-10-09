/**
 * Which shape the panel is: a launcher, or a reading window.
 *
 * Two layouts, because they are answers to different questions. "What do I have
 * about this" wants a narrow list and nothing else on screen. "What does this
 * actually say" wants the message rendered at a readable width, with the list
 * still there beside it so the next one is a keystroke away rather than a
 * navigation.
 *
 * The preference outlives the process — a layout that reset on every launch
 * would be a worse default than either of them.
 */

export type Layout = "stack" | "split";

const KEY = "muster.layout";

export function storedLayout(): Layout {
  try {
    return localStorage.getItem(KEY) === "split" ? "split" : "stack";
  } catch {
    // Private mode, or storage disabled. The narrow layout is the safe default.
    return "stack";
  }
}

export function storeLayout(layout: Layout): void {
  try {
    localStorage.setItem(KEY, layout);
  } catch {
    // Not worth reporting: the layout still applies for this session.
  }
}

/**
 * Resize the window to match. The webview cannot do this itself — the panel is
 * an NSPanel and its size belongs to AppKit, on the main thread.
 */
export async function applyLayout(layout: Layout): Promise<void> {
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("set_layout", { wide: layout === "split" });
  } catch {
    // Running in a browser against the mock fixtures: CSS still does its half.
  }
}


/**
 * Whether the panel stays up when it loses focus.
 *
 * Dismiss-on-blur is right for a launcher and wrong the moment a second window is
 * involved: dragging a file in means clicking Finder first, and that click is the
 * event that hides the panel. So it is a choice, remembered, and off by default —
 * a launcher that lingers is a window, and most of the time this is a launcher.
 *
 * Escape and the hotkey still dismiss it. Pinning suspends the automatic
 * dismissal, never the deliberate one.
 */
const PIN_KEY = "muster.pinned";

export function storedPinned(): boolean {
  try {
    return localStorage.getItem(PIN_KEY) === "1";
  } catch {
    return false;
  }
}

export async function applyPinned(pinned: boolean): Promise<void> {
  try {
    localStorage.setItem(PIN_KEY, pinned ? "1" : "0");
  } catch {
    // Applies for this session regardless.
  }
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("set_pinned", { pinned });
  } catch {
    // A browser against the fixtures: there is no panel to pin.
  }
}
