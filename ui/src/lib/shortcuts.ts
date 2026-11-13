/**
 * Keyboard shortcuts: the chord a keydown stands for, every rebindable
 * action with the chord it ships with, and the rules the shortcuts page
 * checks a new binding against. Free of React and of storage -- what an
 * action is bound to right now (the browser's own overrides) is
 * shortcut-bindings.ts.
 *
 * A chord names a key the way a person reads it off the keyboard, not by
 * its physical position: `KeyZ` is the key printed Z wherever the layout
 * puts it, so `z` means the same key on a German board as on a US one.
 * Characters that need Shift on one layout and not on another (`#`, `!`,
 * `?`, `/`) are chords of the character itself, so Shift is part of the
 * character and never compared on its own.
 */

export interface ShortcutChord {
  /** `KeyA`-`KeyZ` for letters, a single printable character for a symbol,
   * or the physical `KeyboardEvent.code` for anything else (digits,
   * arrows, Enter, F-keys). */
  code: string;
  ctrl: boolean;
  alt: boolean;
  shift: boolean;
  meta: boolean;
}

export type ChordEventLike = Pick<KeyboardEvent, "key" | "code" | "ctrlKey" | "altKey" | "shiftKey" | "metaKey">;

export function chord(code: string, mods: Partial<Omit<ShortcutChord, "code">> = {}): ShortcutChord {
  return { code, ctrl: false, alt: false, shift: false, meta: false, ...mods };
}

/** The layout as the Keyboard Map API reports it: physical `code` to the
 * character that key types unmodified. Chromium only; `null` until it
 * loads and in every other browser, where `event.key` stands in. */
interface LayoutMap {
  get(code: string): string | undefined;
}
let layoutMap: LayoutMap | null = null;

/** Replaces the layout map -- the browser's own loader below, and tests. */
export function setKeyboardLayoutMap(map: LayoutMap | null): void {
  layoutMap = map;
}

function loadKeyboardLayoutMap(): void {
  const keyboard = (navigator as Navigator & {
    keyboard?: { getLayoutMap?: () => Promise<LayoutMap>; addEventListener?: (type: string, cb: () => void) => void };
  }).keyboard;
  // Each member is checked before use: this runs at import, and a throw
  // here would take the whole app down with it.
  if (!keyboard || typeof keyboard.getLayoutMap !== "function") return;
  const getLayoutMap = keyboard.getLayoutMap.bind(keyboard);
  const load = () => {
    getLayoutMap().then(setKeyboardLayoutMap, () => {});
  };
  load();
  if (typeof keyboard.addEventListener === "function") keyboard.addEventListener("layoutchange", load);
}
if (typeof navigator !== "undefined") loadKeyboardLayoutMap();

const LETTER = /^[a-zA-Z]$/;
/** Printable ASCII that is neither a letter, a digit nor a space. */
const SYMBOL = /^[!-/:-@[-`{-~]$/;

function isSymbolCode(code: string): boolean {
  return SYMBOL.test(code);
}

/** The chord code a keydown stands for. A letter comes from the layout map
 * where the browser has one -- `event.key` turns into a symbol under Alt
 * on macOS -- and from `event.key` otherwise. A symbol is the character
 * typed, but only for an unmodified (or Shift-only) press: under Ctrl or
 * Alt the character says more about the modifier than about the key. */
export function chordCode(e: ChordEventLike): string {
  const printed = layoutMap ? layoutMap.get(e.code) : e.key;
  if (printed && LETTER.test(printed)) return `Key${printed.toUpperCase()}`;
  if (!e.ctrlKey && !e.altKey && !e.metaKey && SYMBOL.test(e.key)) return e.key;
  return e.code;
}

export function chordsEqual(a: ShortcutChord, b: ShortcutChord): boolean {
  return (
    a.code === b.code &&
    a.ctrl === b.ctrl &&
    a.alt === b.alt &&
    a.meta === b.meta &&
    (isSymbolCode(a.code) || a.shift === b.shift)
  );
}

export function matchesChord(e: ChordEventLike, target: ShortcutChord): boolean {
  return chordsEqual(chordFromKeys(e), target);
}

function chordFromKeys(e: ChordEventLike): ShortcutChord {
  const code = chordCode(e);
  return {
    code,
    ctrl: e.ctrlKey,
    alt: e.altKey,
    shift: isSymbolCode(code) ? false : e.shiftKey,
    meta: e.metaKey,
  };
}

const MODIFIER_CODES = new Set([
  "ShiftLeft", "ShiftRight", "AltLeft", "AltRight", "AltGraph",
  "ControlLeft", "ControlRight", "MetaLeft", "MetaRight", "CapsLock",
]);

/** The chord a keydown represents while a new binding is being recorded;
 * `null` for a modifier on its own, which is the start of a chord rather
 * than one. */
export function chordFromEvent(e: ChordEventLike): ShortcutChord | null {
  if (MODIFIER_CODES.has(e.code) || e.key === "Dead" || e.key === "Unidentified") return null;
  return chordFromKeys(e);
}

/** Whether Ctrl+Alt produced a character rather than a shortcut: on
 * Windows Ctrl+Alt is AltGr, which types `@`, `{`, `[` and the like on a
 * German board. The key's own character (from the layout map, or the
 * letter or digit its code names) differing from what it typed is the
 * sign. */
export function isAltGrCharacter(e: ChordEventLike): boolean {
  if (!(e.ctrlKey && e.altKey) || e.key.length !== 1) return false;
  let plain = layoutMap?.get(e.code);
  if (plain === undefined) {
    if (e.code.startsWith("Key")) plain = e.code.slice(3);
    else if (e.code.startsWith("Digit")) plain = e.code.slice(5);
    else return true;
  }
  return e.key.toLowerCase() !== plain.toLowerCase();
}

/** Whether a focused text field keeps this keystroke instead of the
 * shortcut. A field keeps anything it could type, and Ctrl/Cmd without
 * Alt, which a rich-text editor uses for bold, italics and links. A
 * Ctrl+Alt chord still acts from a field, unless it is AltGr typing a
 * character there. */
export function yieldsToField(e: ChordEventLike, c: ShortcutChord): boolean {
  return !c.alt || isAltGrCharacter(e);
}

export type ShortcutScope = "global" | "mail" | "calendar" | "orders";

export const SCOPE_LABELS: Record<ShortcutScope, string> = {
  global: "Anywhere",
  mail: "Mail list and reading pane",
  calendar: "Calendar",
  orders: "Orders & tickets",
};

export const SCOPE_ORDER: ShortcutScope[] = ["global", "mail", "calendar", "orders"];

export interface ShortcutAction {
  id: string;
  label: string;
  scope: ShortcutScope;
  default: ShortcutChord;
}

const ctrlAlt = (code: string) => chord(code, { ctrl: true, alt: true });

/** Every rebindable action, in the order the shortcuts page lists them. */
export const SHORTCUT_ACTIONS = [
  { id: "go_mail", label: "Go to mail", scope: "global", default: ctrlAlt("Digit1") },
  { id: "go_calendar", label: "Go to calendar", scope: "global", default: ctrlAlt("Digit2") },
  { id: "go_contacts", label: "Go to contacts", scope: "global", default: ctrlAlt("Digit3") },
  { id: "go_orders", label: "Go to orders & tickets", scope: "global", default: ctrlAlt("Digit4") },
  { id: "go_spam_review", label: "Go to spam review", scope: "global", default: ctrlAlt("Digit5") },
  { id: "go_search", label: "Go to the search page", scope: "global", default: ctrlAlt("Digit6") },
  { id: "go_accounts", label: "Go to accounts", scope: "global", default: ctrlAlt("Digit7") },
  { id: "go_pipeline", label: "Go to admin processing", scope: "global", default: ctrlAlt("Digit8") },
  { id: "go_settings", label: "Go to settings", scope: "global", default: ctrlAlt("Digit9") },
  { id: "focus_search", label: "Focus search", scope: "global", default: chord("/") },
  { id: "toggle_sidebar", label: "Show or hide the sidebar", scope: "global", default: chord("KeyB", { ctrl: true }) },
  { id: "show_shortcuts", label: "Show the shortcut cheat sheet", scope: "global", default: chord("?") },

  { id: "next_message", label: "Next message", scope: "mail", default: chord("KeyJ") },
  { id: "previous_message", label: "Previous message", scope: "mail", default: chord("KeyK") },
  { id: "toggle_select", label: "Select or deselect the current message", scope: "mail", default: chord("KeyX") },
  { id: "compose", label: "Compose a new message", scope: "mail", default: chord("KeyC") },
  { id: "reply", label: "Reply", scope: "mail", default: chord("KeyR", { shift: true }) },
  { id: "reply_all", label: "Reply all", scope: "mail", default: chord("KeyA") },
  { id: "forward", label: "Forward", scope: "mail", default: chord("KeyF") },
  { id: "move", label: "Move to folder", scope: "mail", default: chord("KeyV") },
  { id: "archive", label: "Archive", scope: "mail", default: chord("KeyE") },
  { id: "trash", label: "Move to trash", scope: "mail", default: chord("#") },
  { id: "spam", label: "Mark as spam", scope: "mail", default: chord("!") },
  { id: "toggle_read", label: "Toggle read / unread", scope: "mail", default: chord("KeyR") },
  { id: "mark_unread", label: "Mark as unread", scope: "mail", default: chord("KeyU") },
  { id: "toggle_star", label: "Toggle star", scope: "mail", default: chord("KeyS") },

  { id: "calendar_today", label: "Jump to today", scope: "calendar", default: chord("KeyT") },
  { id: "calendar_day", label: "Day view", scope: "calendar", default: chord("KeyD") },
  { id: "calendar_week", label: "Week view", scope: "calendar", default: chord("KeyW") },
  { id: "calendar_month", label: "Month view", scope: "calendar", default: chord("KeyM") },
  { id: "calendar_agenda", label: "Agenda view", scope: "calendar", default: chord("KeyA") },
  { id: "calendar_new_event", label: "New event", scope: "calendar", default: chord("KeyN") },
  { id: "calendar_next", label: "Next period", scope: "calendar", default: chord("KeyJ") },
  { id: "calendar_previous", label: "Previous period", scope: "calendar", default: chord("KeyK") },

  { id: "next_order", label: "Next order or ticket", scope: "orders", default: chord("KeyJ") },
  { id: "previous_order", label: "Previous order or ticket", scope: "orders", default: chord("KeyK") },
] as const satisfies readonly ShortcutAction[];

export type ShortcutActionId = (typeof SHORTCUT_ACTIONS)[number]["id"];

const ACTION_BY_ID = new Map<string, ShortcutAction>(SHORTCUT_ACTIONS.map((a) => [a.id, a]));

export function shortcutAction(id: ShortcutActionId): ShortcutAction {
  return ACTION_BY_ID.get(id)!;
}

/** Keys a scope handles itself and that cannot be rebound -- listed on the
 * shortcuts page and in the cheat sheet beside the rebindable ones. */
export const FIXED_SHORTCUTS: Record<ShortcutScope, Array<{ keys: string; label: string }>> = {
  global: [{ keys: "Ctrl+F / ⌘F", label: "Find in the open message" }],
  mail: [
    { keys: "↓ / ↑", label: "Next / previous message" },
    { keys: "Enter", label: "Open the focused message" },
    { keys: "Esc", label: "Close the reading pane / clear selection" },
    { keys: "Delete", label: "Move to trash" },
    { keys: "Ctrl+Z / ⌘Z", label: "Undo the last mail action" },
  ],
  calendar: [
    { keys: "→ / ←", label: "Next / previous period" },
    { keys: "Delete", label: "Delete the selected event" },
    { keys: "Esc", label: "Close the event popover" },
  ],
  orders: [],
};

/** A stored override: a chord, or `null` for an action switched off. */
export type ShortcutOverrides = Partial<Record<ShortcutActionId, ShortcutChord | null>>;
export type ShortcutBindings = Record<ShortcutActionId, ShortcutChord | null>;

export function effectiveBindings(overrides: ShortcutOverrides): ShortcutBindings {
  return Object.fromEntries(
    SHORTCUT_ACTIONS.map((a) => [a.id, a.id in overrides ? (overrides[a.id] ?? null) : a.default]),
  ) as ShortcutBindings;
}

/** The action of `scope` a keydown triggers, if any. */
export function matchAction(
  e: ChordEventLike,
  bindings: ShortcutBindings,
  scope: ShortcutScope,
): ShortcutActionId | null {
  const pressed = chordFromKeys(e);
  for (const action of SHORTCUT_ACTIONS) {
    if (action.scope !== scope) continue;
    const bound = bindings[action.id];
    if (bound && chordsEqual(pressed, bound)) return action.id;
  }
  return null;
}

const BROWSER_KEYS = new Set(["KeyC", "KeyV", "KeyX", "KeyA", "KeyZ", "KeyY", "KeyF", "KeyW", "KeyT", "KeyN", "KeyR", "KeyL", "KeyQ", "Tab"]);
const BARE_FIXED = new Set(["Escape", "Enter", "Tab", "Delete", "Backspace", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Space"]);

/** Why a chord cannot be bound at all, or `null` when it can. */
export function reservedReason(c: ShortcutChord): string | null {
  const bare = !c.ctrl && !c.alt && !c.meta;
  if (bare && !c.shift && BARE_FIXED.has(c.code)) return "This key already has a fixed job on the page.";
  if (bare && c.shift && c.code === "Tab") return "Shift+Tab moves focus backwards.";
  if ((c.ctrl || c.meta) && !c.alt && BROWSER_KEYS.has(c.code)) {
    return "The browser or text editing keeps this one.";
  }
  return null;
}

/** The action already bound to `candidate` where it would also fire: in
 * the same scope, or anywhere when either side is global. */
export function findConflict(
  candidate: ShortcutChord,
  forId: ShortcutActionId,
  bindings: ShortcutBindings,
): ShortcutActionId | null {
  const scope = shortcutAction(forId).scope;
  for (const action of SHORTCUT_ACTIONS) {
    if (action.id === forId) continue;
    if (scope !== "global" && action.scope !== "global" && action.scope !== scope) continue;
    const bound = bindings[action.id];
    if (bound && chordsEqual(bound, candidate)) return action.id;
  }
  return null;
}

/** Overrides with `id` set to `next`, or dropped when `next` is its default
 * -- the stored map holds only real differences, so a default changed in a
 * later release reaches everyone who never touched it. */
export function withOverride(
  overrides: ShortcutOverrides,
  id: ShortcutActionId,
  next: ShortcutChord | null,
): ShortcutOverrides {
  const updated = { ...overrides };
  const def = shortcutAction(id).default;
  if (next && chordsEqual(next, def)) delete updated[id];
  else updated[id] = next;
  return updated;
}

/** Drops whatever a stored value holds that is not a known action with a
 * well-formed chord -- an action removed from the registry, or a value
 * someone edited by hand. */
export function sanitizeOverrides(raw: unknown): ShortcutOverrides {
  if (!raw || typeof raw !== "object") return {};
  const out: ShortcutOverrides = {};
  for (const [id, value] of Object.entries(raw as Record<string, unknown>)) {
    if (!ACTION_BY_ID.has(id)) continue;
    if (value === null) {
      out[id as ShortcutActionId] = null;
      continue;
    }
    const v = value as Partial<ShortcutChord>;
    if (
      typeof v.code === "string" && v.code.length > 0 &&
      typeof v.ctrl === "boolean" && typeof v.alt === "boolean" &&
      typeof v.shift === "boolean" && typeof v.meta === "boolean"
    ) {
      out[id as ShortcutActionId] = chord(v.code, { ctrl: v.ctrl, alt: v.alt, shift: v.shift, meta: v.meta });
    }
  }
  return out;
}

const KEY_LABELS: Record<string, string> = {
  ArrowUp: "↑", ArrowDown: "↓", ArrowLeft: "←", ArrowRight: "→",
  Escape: "Esc", Backquote: "`", Minus: "-", Equal: "=", BracketLeft: "[", BracketRight: "]",
  Backslash: "\\", IntlBackslash: "<", Semicolon: ";", Quote: "'", Comma: ",", Period: ".", Slash: "/",
};

function keyLabel(code: string): string {
  if (code.startsWith("Key") && code.length === 4) return code.slice(3);
  if (code.startsWith("Digit")) return code.slice(5);
  if (code.startsWith("Numpad")) return `Num ${code.slice(6)}`;
  if (isSymbolCode(code)) return code;
  // What the key prints on this layout -- `ü` rather than BracketLeft on
  // a German board -- where the browser can say.
  const printed = layoutMap?.get(code);
  if (printed && printed.length === 1 && printed !== " ") return printed.toUpperCase();
  return KEY_LABELS[code] ?? code;
}

/** "Ctrl+Alt+1", "Shift+R", "#". A lone letter reads lower-case, the way
 * it is typed. */
export function formatChord(c: ShortcutChord): string {
  const parts: string[] = [];
  if (c.ctrl) parts.push("Ctrl");
  if (c.alt) parts.push("Alt");
  if (c.shift) parts.push("Shift");
  if (c.meta) parts.push("⌘");
  const key = keyLabel(c.code);
  parts.push(parts.length === 0 && c.code.startsWith("Key") ? key.toLowerCase() : key);
  return parts.join("+");
}
