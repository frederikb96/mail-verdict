/**
 * The pure half of the settings forms: edits are held beside the server's
 * values, keyed by category and setting name, and a key counts as changed
 * only while its edit differs from what the server holds -- typing a value
 * back to its original leaves nothing to save.
 */

export type SettingsDocument = Record<string, Record<string, unknown>>;

/** Edits keyed `category.key`. */
export type SettingEdits = Record<string, unknown>;

export function editKey(category: string, key: string): string {
  return `${category}.${key}`;
}

function splitEditKey(composite: string): [string, string] {
  const at = composite.indexOf(".");
  return [composite.slice(0, at), composite.slice(at + 1)];
}

function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

/** The value a field shows: its edit if there is one, else the server's. */
export function draftValue(
  server: SettingsDocument | undefined,
  edits: SettingEdits,
  category: string,
  key: string,
): unknown {
  const composite = editKey(category, key);
  return composite in edits ? edits[composite] : server?.[category]?.[key];
}

/** The edits that actually differ from the server, by category. */
export function changedByCategory(
  server: SettingsDocument | undefined,
  edits: SettingEdits,
): Record<string, Record<string, unknown>> {
  const out: Record<string, Record<string, unknown>> = {};
  for (const [composite, value] of Object.entries(edits)) {
    const [category, key] = splitEditKey(composite);
    if (sameValue(value, server?.[category]?.[key])) continue;
    (out[category] ??= {})[key] = value;
  }
  return out;
}

export function changedCount(
  server: SettingsDocument | undefined,
  edits: SettingEdits,
): number {
  return Object.values(changedByCategory(server, edits)).reduce(
    (n, keys) => n + Object.keys(keys).length,
    0,
  );
}

/** The edits left once these categories were saved. */
export function withoutCategories(edits: SettingEdits, saved: string[]): SettingEdits {
  const out: SettingEdits = {};
  for (const [composite, value] of Object.entries(edits)) {
    if (!saved.includes(splitEditKey(composite)[0])) out[composite] = value;
  }
  return out;
}
