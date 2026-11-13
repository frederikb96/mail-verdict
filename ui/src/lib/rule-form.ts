/**
 * The rule editor's form model and its two-way conversion with a stage's
 * `when` tree and `effects` list.
 *
 * Nothing is ever dropped silently: `parseWhen` and `parseEffects` return
 * null for anything the form cannot show exactly (an unknown key, a tree
 * nested deeper than one group, a flag the form does not offer), and the
 * editor then opens in JSON mode with a notice instead.
 */

export type Combinator = "all" | "any";

export type ConditionKind =
  | "sender_match"
  | "sender_domain"
  | "subject_contains"
  | "subject_matches"
  | "body_contains"
  | "body_matches"
  | "header_exists"
  | "header_match"
  | "attachment_any"
  | "attachment_type"
  | "size_gt"
  | "size_lt"
  | "folder_is"
  | "tag_is"
  | "verdict_spam"
  | "verdict_ham";

export type SizeUnit = "KB" | "MB";

export interface ConditionRow {
  id: string;
  kind: ConditionKind;
  /** The text, regex, tag, folder, type or number the kind takes. */
  value: string;
  /** The header name, for the two header kinds. */
  field: string;
  unit: SizeUnit;
  /** "is not": the row is wrapped in a `not`. */
  negate: boolean;
}

export interface ConditionGroup {
  combinator: Combinator;
  rows: ConditionRow[];
}

export type ConditionItem =
  | { type: "row"; row: ConditionRow }
  | { type: "group"; group: ConditionGroup };

export interface WhenForm {
  combinator: Combinator;
  items: ConditionItem[];
}

export type ActionKind =
  | "move"
  | "seen"
  | "unseen"
  | "star"
  | "unstar"
  | "keyword"
  | "tag"
  | "trash"
  | "expunge"
  | "notify";

export interface ActionRow {
  id: string;
  kind: ActionKind;
  /** Folder reference (`use:junk`, `id:<uuid>`, `name:<imap name>`), keyword, tag or notification text. */
  value: string;
}

type Json = Record<string, unknown>;

const KB = 1024;
const MB = 1024 * 1024;

let counter = 0;
export function newId(): string {
  counter += 1;
  return `f${counter}`;
}

function isObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function emptyConditionRow(kind: ConditionKind = "subject_contains"): ConditionRow {
  return { id: newId(), kind, value: "", field: "", unit: "MB", negate: false };
}

export function emptyAction(kind: ActionKind = "move"): ActionRow {
  return { id: newId(), kind, value: "" };
}

// --- Conditions ---------------------------------------------------------

function parseLeaf(raw: unknown, negate: boolean): ConditionRow | null {
  if (!isObject(raw)) return null;
  const keys = Object.keys(raw);
  if (keys.length !== 1) return null;
  const key = keys[0];
  const value = raw[key];
  const row = emptyConditionRow();
  row.negate = negate;
  switch (key) {
    case "sender_match":
    case "sender_domain":
    case "subject_contains":
    case "subject_matches":
    case "body_contains":
    case "body_matches":
    case "header_exists":
    case "folder_is":
    case "tag_is":
      if (typeof value !== "string") return null;
      row.kind = key;
      row.value = value;
      return row;
    case "header_match": {
      if (!isObject(value)) return null;
      const extra = Object.keys(value).filter((k) => k !== "field" && k !== "pattern");
      if (extra.length > 0 || typeof value.field !== "string" || typeof value.pattern !== "string") {
        return null;
      }
      row.kind = "header_match";
      row.field = value.field;
      row.value = value.pattern;
      return row;
    }
    case "size_gt":
    case "size_lt": {
      if (typeof value !== "number" || !Number.isInteger(value) || value < 0) return null;
      if (value % MB === 0) {
        row.unit = "MB";
        row.value = String(value / MB);
      } else if (value % KB === 0) {
        row.unit = "KB";
        row.value = String(value / KB);
      } else {
        return null;
      }
      row.kind = key;
      return row;
    }
    case "has_attachment":
      if (value === true) {
        row.kind = "attachment_any";
        return row;
      }
      if (typeof value === "string" && value) {
        row.kind = "attachment_type";
        row.value = value;
        return row;
      }
      return null;
    case "verdict_is":
      if (value === "spam") row.kind = "verdict_spam";
      else if (value === "not-spam") row.kind = "verdict_ham";
      else return null;
      return row;
    default:
      return null;
  }
}

function parseRow(raw: unknown): ConditionRow | null {
  if (isObject(raw) && Object.keys(raw).length === 1 && "not" in raw) {
    return parseLeaf(raw.not, true);
  }
  return parseLeaf(raw, false);
}

function combinatorOf(raw: Json): Combinator | null {
  if (Object.keys(raw).length !== 1) return null;
  return Array.isArray(raw.all) ? "all" : Array.isArray(raw.any) ? "any" : null;
}

function parseGroup(raw: unknown): ConditionGroup | null {
  if (!isObject(raw)) return null;
  const combinator = combinatorOf(raw);
  if (combinator === null) return null;
  const rows: ConditionRow[] = [];
  for (const entry of raw[combinator] as unknown[]) {
    const row = parseRow(entry);
    if (row === null) return null;
    rows.push(row);
  }
  return { combinator, rows };
}

/** The `when` tree as a form, or null when the form cannot show it exactly. */
export function parseWhen(raw: unknown): WhenForm | null {
  if (raw === undefined || raw === null) return { combinator: "all", items: [] };
  if (!isObject(raw)) return null;
  if (Object.keys(raw).length === 0) return { combinator: "all", items: [] };
  const combinator = combinatorOf(raw);
  if (combinator === null) {
    const row = parseRow(raw);
    return row === null ? null : { combinator: "all", items: [{ type: "row", row }] };
  }
  const items: ConditionItem[] = [];
  for (const entry of raw[combinator] as unknown[]) {
    const row = parseRow(entry);
    if (row !== null) {
      items.push({ type: "row", row });
      continue;
    }
    const group = parseGroup(entry);
    if (group === null) return null;
    items.push({ type: "group", group });
  }
  return { combinator, items };
}

function buildLeaf(row: ConditionRow): Json {
  const value = row.value.trim();
  let leaf: Json;
  switch (row.kind) {
    case "header_match":
      leaf = { header_match: { field: row.field.trim(), pattern: row.value } };
      break;
    case "size_gt":
    case "size_lt":
      leaf = { [row.kind]: Math.round(Number(value) * (row.unit === "MB" ? MB : KB)) };
      break;
    case "attachment_any":
      leaf = { has_attachment: true };
      break;
    case "attachment_type":
      leaf = { has_attachment: value };
      break;
    case "verdict_spam":
      leaf = { verdict_is: "spam" };
      break;
    case "verdict_ham":
      leaf = { verdict_is: "not-spam" };
      break;
    case "subject_matches":
    case "body_matches":
      leaf = { [row.kind]: row.value };
      break;
    default:
      leaf = { [row.kind]: value };
  }
  return row.negate ? { not: leaf } : leaf;
}

function buildCombined(combinator: Combinator, parts: Json[]): Json {
  if (parts.length === 1) return parts[0];
  return { [combinator]: parts };
}

export function buildWhen(form: WhenForm): Json {
  if (form.items.length === 0) return {};
  const parts = form.items.map((item) =>
    item.type === "row"
      ? buildLeaf(item.row)
      : buildCombined(item.group.combinator, item.group.rows.map(buildLeaf)),
  );
  return buildCombined(form.combinator, parts);
}

/** What is wrong with one condition row, or null. Regexes are compiled here
 * so a typo shows before the server's own check; the server stays the judge. */
export function conditionError(row: ConditionRow): string | null {
  switch (row.kind) {
    case "attachment_any":
    case "verdict_spam":
    case "verdict_ham":
      return null;
    case "header_match":
      if (!row.field.trim()) return "Name the header";
      return regexError(row.value);
    case "header_exists":
      return row.value.trim() ? null : "Name the header";
    case "subject_matches":
    case "body_matches":
      return regexError(row.value);
    case "size_gt":
    case "size_lt": {
      const n = Number(row.value);
      return row.value.trim() && Number.isFinite(n) && n >= 0 ? null : "Enter a size";
    }
    default:
      return row.value.trim() ? null : "Enter a value";
  }
}

function regexError(pattern: string): string | null {
  if (!pattern) return "Enter a pattern";
  try {
    new RegExp(pattern, "i");
    return null;
  } catch (err) {
    return err instanceof Error ? err.message.replace(/^Invalid regular expression: /, "") : "Not a valid pattern";
  }
}

// --- Actions ------------------------------------------------------------

export function parseEffects(raw: unknown): ActionRow[] | null {
  if (raw === undefined || raw === null) return [];
  if (!Array.isArray(raw)) return null;
  const rows: ActionRow[] = [];
  for (const effect of raw) {
    if (!isObject(effect) || Object.keys(effect).length !== 1) return null;
    const kind = Object.keys(effect)[0];
    const body = isObject(effect[kind]) ? (effect[kind] as Json) : {};
    switch (kind) {
      case "move": {
        const refs = (["folder_id", "folder_name", "special_use"] as const).filter(
          (k) => typeof body[k] === "string" && body[k],
        );
        const extra = Object.keys(body).filter(
          (k) => !["folder_id", "folder_name", "special_use"].includes(k) && body[k] != null,
        );
        if (refs.length !== 1 || extra.length > 0) return null;
        const prefix = { folder_id: "id", folder_name: "name", special_use: "use" }[refs[0]];
        rows.push({ id: newId(), kind: "move", value: `${prefix}:${body[refs[0]] as string}` });
        break;
      }
      case "trash":
      case "expunge":
        rows.push({ id: newId(), kind, value: "" });
        break;
      case "notify":
        if (typeof body.text !== "string") return null;
        rows.push({ id: newId(), kind: "notify", value: body.text });
        break;
      case "set_flags": {
        const flags = Object.entries(body).filter(([, v]) => v !== null && v !== undefined);
        for (const [flag, v] of flags) {
          if ((flag !== "seen" && flag !== "flagged") || typeof v !== "boolean") return null;
          const rowKind: ActionKind = flag === "seen" ? (v ? "seen" : "unseen") : v ? "star" : "unstar";
          rows.push({ id: newId(), kind: rowKind, value: "" });
        }
        if (flags.length === 0) return null;
        break;
      }
      case "keywords":
      case "tag": {
        const add = Array.isArray(body.add) ? body.add : [];
        const remove = Array.isArray(body.remove) ? body.remove : [];
        if (remove.length > 0 || add.length === 0 || add.some((x) => typeof x !== "string")) return null;
        for (const item of add as string[]) {
          rows.push({ id: newId(), kind: kind === "tag" ? "tag" : "keyword", value: item });
        }
        break;
      }
      default:
        return null;
    }
  }
  return rows;
}

export function buildEffects(rows: ActionRow[]): Json[] {
  return rows.map((row): Json => {
    switch (row.kind) {
      case "move": {
        const sep = row.value.indexOf(":");
        const prefix = row.value.slice(0, sep);
        const rest = row.value.slice(sep + 1);
        const key = { id: "folder_id", name: "folder_name", use: "special_use" }[prefix] ?? "folder_name";
        return { move: { [key]: rest } };
      }
      case "seen":
        return { set_flags: { seen: true } };
      case "unseen":
        return { set_flags: { seen: false } };
      case "star":
        return { set_flags: { flagged: true } };
      case "unstar":
        return { set_flags: { flagged: false } };
      case "keyword":
        return { keywords: { add: [row.value.trim()] } };
      case "tag":
        return { tag: { add: [row.value.trim()] } };
      case "trash":
        return { trash: {} };
      case "expunge":
        return { expunge: {} };
      case "notify":
        return { notify: { text: row.value.trim() } };
    }
  });
}

export function actionError(row: ActionRow): string | null {
  switch (row.kind) {
    case "move":
      return row.value.includes(":") && row.value.slice(row.value.indexOf(":") + 1) ? null : "Choose a folder";
    case "keyword":
    case "tag":
    case "notify":
      return row.value.trim() ? null : "Enter a value";
    default:
      return null;
  }
}
