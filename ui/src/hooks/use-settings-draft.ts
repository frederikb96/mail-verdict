/**
 * Unsaved edits to the settings document, shared by every form that has a
 * save bar. A save writes each touched category with its own PUT and only
 * the keys that changed; a category the server refuses keeps its edits and
 * carries the server's message.
 */

import { useCallback, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { settingsKeys, useAllSettings } from "@/hooks/use-settings";
import {
  changedByCategory,
  draftValue,
  editKey,
  withoutCategories,
  type SettingEdits,
} from "@/lib/settings-draft";

export interface SettingsDraft {
  /** False until the server's settings have arrived. */
  ready: boolean;
  value: (category: string, key: string) => unknown;
  /** Whether the server returned this key at all. */
  has: (category: string, key: string) => boolean;
  set: (category: string, key: string, value: unknown) => void;
  /** Whether this key's edit differs from the server. */
  isChanged: (category: string, key: string) => boolean;
  /** Changed keys, optionally within these categories only. */
  count: (categories?: string[]) => number;
  saving: boolean;
  /** The server's refusal, by category. */
  errors: Record<string, string>;
  save: (categories?: string[]) => Promise<void>;
  discard: (categories?: string[]) => void;
}

export function useSettingsDraft(): SettingsDraft {
  const { data: server } = useAllSettings();
  const qc = useQueryClient();
  const [edits, setEdits] = useState<SettingEdits>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);

  const value = useCallback(
    (category: string, key: string) => draftValue(server, edits, category, key),
    [server, edits],
  );

  const has = useCallback(
    (category: string, key: string) => key in (server?.[category] ?? {}),
    [server],
  );

  const set = useCallback((category: string, key: string, next: unknown) => {
    setEdits((prev) => ({ ...prev, [editKey(category, key)]: next }));
    setErrors((prev) => (category in prev ? { ...prev, [category]: "" } : prev));
  }, []);

  const changed = useMemo(() => changedByCategory(server, edits), [server, edits]);

  const save = useCallback(
    async (categories?: string[]) => {
      const targets = Object.keys(changed).filter(
        (c) => !categories || categories.includes(c),
      );
      if (targets.length === 0) return;
      setSaving(true);
      const saved: string[] = [];
      const failures: Record<string, string> = {};
      for (const category of targets) {
        try {
          await api.settings.update(category, changed[category]);
          saved.push(category);
        } catch (err) {
          failures[category] = err instanceof Error ? err.message : "The request failed";
        }
      }
      setEdits((prev) => withoutCategories(prev, saved));
      setErrors((prev) => ({ ...prev, ...Object.fromEntries(saved.map((c) => [c, ""])), ...failures }));
      setSaving(false);
      if (saved.length > 0) {
        await qc.invalidateQueries({ queryKey: settingsKeys.all });
      }
    },
    [changed, qc],
  );

  const discard = useCallback((categories?: string[]) => {
    setEdits((prev) => (categories ? withoutCategories(prev, categories) : {}));
    setErrors({});
  }, []);

  return {
    ready: server !== undefined,
    value,
    has,
    set,
    isChanged: (category, key) => key in (changed[category] ?? {}),
    count: (categories) =>
      Object.entries(changed)
        .filter(([c]) => !categories || categories.includes(c))
        .reduce((n, [, keys]) => n + Object.keys(keys).length, 0),
    saving,
    errors,
    save,
    discard,
  };
}
