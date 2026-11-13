/**
 * What every shortcut is bound to in this browser: the registry's defaults
 * (lib/shortcuts.ts) with the overrides set on the shortcuts page laid
 * over them. Kept in localStorage per signed-in user, like the other view
 * preferences.
 */

"use client";

import { useMemo } from "react";
import { useAtomValue } from "jotai";
import { userAtomWithStorage } from "@/lib/user-storage";
import { effectiveBindings, sanitizeOverrides, type ShortcutBindings, type ShortcutOverrides } from "@/lib/shortcuts";

/** Written by the shortcuts page only; a stored value is sanitized on the
 * way in, so a hand-edited or outdated entry falls back to its default. */
export const shortcutOverridesAtom = userAtomWithStorage<ShortcutOverrides>("mailverdict:shortcuts", {}, {
  readTransform: sanitizeOverrides,
});

export function useShortcutBindings(): ShortcutBindings {
  const overrides = useAtomValue(shortcutOverridesAtom);
  return useMemo(() => effectiveBindings(overrides), [overrides]);
}
