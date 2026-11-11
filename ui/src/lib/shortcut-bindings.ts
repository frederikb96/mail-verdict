/**
 * What every shortcut is bound to in this browser: the registry's defaults
 * (lib/shortcuts.ts) with the overrides set on the shortcuts page laid
 * over them. Per device in localStorage, like the theme and the other
 * view preferences -- a binding belongs to the keyboard in front of
 * someone, not to the mailbox.
 */

"use client";

import { useMemo } from "react";
import { useAtomValue } from "jotai";
import { atomWithStorage, createJSONStorage } from "jotai/utils";
import { effectiveBindings, sanitizeOverrides, type ShortcutBindings, type ShortcutOverrides } from "@/lib/shortcuts";

const storage = createJSONStorage<ShortcutOverrides>(() => localStorage);

/** Written by the shortcuts page only; a stored value is sanitized on the
 * way in, so a hand-edited or outdated entry falls back to its default. */
export const shortcutOverridesAtom = atomWithStorage<ShortcutOverrides>("mailverdict:shortcuts", {}, {
  ...storage,
  getItem: (key, initial) => sanitizeOverrides(storage.getItem(key, initial)),
});

export function useShortcutBindings(): ShortcutBindings {
  const overrides = useAtomValue(shortcutOverridesAtom);
  return useMemo(() => effectiveBindings(overrides), [overrides]);
}
