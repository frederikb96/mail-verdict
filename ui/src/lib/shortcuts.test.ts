import { afterEach, test } from "node:test";
import assert from "node:assert/strict";
import {
  chord,
  chordFromEvent,
  effectiveBindings,
  findConflict,
  formatChord,
  isAltGrCharacter,
  matchAction,
  reservedReason,
  sanitizeOverrides,
  setKeyboardLayoutMap,
  withOverride,
  yieldsToField,
  type ChordEventLike,
} from "./shortcuts.ts";

/** The keys of a German QWERTZ board that differ from US QWERTY. */
const GERMAN = new Map([
  ["KeyY", "z"], ["KeyZ", "y"], ["KeyQ", "q"], ["KeyR", "r"], ["KeyJ", "j"],
  ["Minus", "ß"], ["Backslash", "#"], ["Digit1", "1"], ["Digit7", "7"],
]);

function key(code: string, keyValue: string, mods: Partial<ChordEventLike> = {}): ChordEventLike {
  return { code, key: keyValue, ctrlKey: false, altKey: false, shiftKey: false, metaKey: false, ...mods };
}

const defaults = effectiveBindings({});

afterEach(() => setKeyboardLayoutMap(null));

test("a letter is the key printed with it, wherever the layout puts it", () => {
  setKeyboardLayoutMap(GERMAN);
  // The German Z sits where a US board has Y.
  assert.deepEqual(chordFromEvent(key("KeyY", "z")), chord("KeyZ"));
  assert.deepEqual(chordFromEvent(key("KeyY", "Z", { shiftKey: true })), chord("KeyZ", { shift: true }));
});

test("without a layout map the typed letter decides", () => {
  assert.deepEqual(chordFromEvent(key("KeyY", "z")), chord("KeyZ"));
});

test("a letter under Alt is still that letter where macOS types a symbol", () => {
  setKeyboardLayoutMap(GERMAN);
  assert.deepEqual(chordFromEvent(key("KeyY", "Ω", { altKey: true })), chord("KeyZ", { alt: true }));
});

test("a symbol matches on either layout, Shift or not", () => {
  // # is its own key on a German board and Shift+3 on a US one.
  assert.equal(matchAction(key("Backslash", "#"), defaults, "mail"), "trash");
  assert.equal(matchAction(key("Digit3", "#", { shiftKey: true }), defaults, "mail"), "trash");
  assert.equal(matchAction(key("Digit1", "!", { shiftKey: true }), defaults, "mail"), "spam");
  // ? is Shift+ß on a German board.
  assert.equal(matchAction(key("Minus", "?", { shiftKey: true }), defaults, "global"), "show_shortcuts");
});

test("Ctrl+Alt+digit goes by the digit key", () => {
  assert.equal(matchAction(key("Digit4", "4", { ctrlKey: true, altKey: true }), defaults, "global"), "go_orders");
  assert.equal(matchAction(key("Digit5", "5", { ctrlKey: true, altKey: true }), defaults, "global"), "go_spam_review");
});

test("modifiers are compared exactly", () => {
  assert.equal(matchAction(key("KeyR", "r"), defaults, "mail"), "toggle_read");
  assert.equal(matchAction(key("KeyR", "R", { shiftKey: true }), defaults, "mail"), "reply");
  assert.equal(matchAction(key("KeyR", "r", { ctrlKey: true }), defaults, "mail"), null);
  assert.equal(matchAction(key("KeyD", "d", { ctrlKey: true }), defaults, "calendar"), null);
});

test("an action switched off matches nothing", () => {
  const bindings = effectiveBindings({ archive: null });
  assert.equal(matchAction(key("KeyE", "e"), bindings, "mail"), null);
});

test("AltGr typing a character is recognised, a Ctrl+Alt shortcut is not", () => {
  setKeyboardLayoutMap(GERMAN);
  // AltGr+Q types @ on a German board; Windows reports it as Ctrl+Alt.
  assert.equal(isAltGrCharacter(key("KeyQ", "@", { ctrlKey: true, altKey: true })), true);
  assert.equal(isAltGrCharacter(key("Digit7", "{", { ctrlKey: true, altKey: true })), true);
  assert.equal(isAltGrCharacter(key("Digit1", "1", { ctrlKey: true, altKey: true })), false);
  const ctrlAlt7 = chord("Digit7", { ctrl: true, alt: true });
  assert.equal(yieldsToField(key("Digit7", "{", { ctrlKey: true, altKey: true }), ctrlAlt7), true);
  assert.equal(yieldsToField(key("Digit7", "7", { ctrlKey: true, altKey: true }), ctrlAlt7), false);
});

test("a text field keeps typed keys and editor chords like Ctrl+B", () => {
  assert.equal(yieldsToField(key("KeyB", "b", { ctrlKey: true }), chord("KeyB", { ctrl: true })), true);
  assert.equal(yieldsToField(key("Slash", "/"), chord("/")), true);
});

test("conflicts are found within a scope and against global, not across pages", () => {
  // a is reply all in mail and agenda view in the calendar: never both live.
  assert.equal(findConflict(chord("KeyA"), "calendar_agenda", defaults), null);
  assert.equal(findConflict(chord("KeyE"), "toggle_star", defaults), "archive");
  assert.equal(findConflict(chord("Digit1", { ctrl: true, alt: true }), "archive", defaults), "go_mail");
  assert.equal(findConflict(chord("KeyE"), "focus_search", defaults), "archive");
});

test("keys with a fixed job or owned by the browser cannot be bound", () => {
  assert.notEqual(reservedReason(chord("Enter")), null);
  assert.notEqual(reservedReason(chord("KeyC", { ctrl: true })), null);
  assert.equal(reservedReason(chord("KeyC", { ctrl: true, alt: true })), null);
  assert.equal(reservedReason(chord("KeyG")), null);
});

test("an override equal to the default is not stored", () => {
  const changed = withOverride({}, "archive", chord("KeyY"));
  assert.deepEqual(changed, { archive: chord("KeyY") });
  assert.deepEqual(withOverride(changed, "archive", chord("KeyE")), {});
  assert.deepEqual(withOverride({}, "archive", null), { archive: null });
});

test("stored overrides keep only known actions with well-formed chords", () => {
  const raw = {
    archive: { code: "KeyY", ctrl: false, alt: false, shift: false, meta: false },
    spam: null,
    gone_action: { code: "KeyQ", ctrl: false, alt: false, shift: false, meta: false },
    trash: { code: 5 },
  };
  assert.deepEqual(sanitizeOverrides(raw), { archive: chord("KeyY"), spam: null });
  assert.deepEqual(sanitizeOverrides("garbage"), {});
});

test("chords read the way they are typed", () => {
  assert.equal(formatChord(chord("KeyE")), "e");
  assert.equal(formatChord(chord("KeyR", { shift: true })), "Shift+R");
  assert.equal(formatChord(chord("Digit4", { ctrl: true, alt: true })), "Ctrl+Alt+4");
  assert.equal(formatChord(chord("#")), "#");
});
