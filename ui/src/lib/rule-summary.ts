/**
 * One sentence per rule, and the group a rule is listed under.
 *
 * The vocabulary is the closed set the server evaluates (`rules/conditions.py`
 * conditions, `pipeline/effect_codec.py` effects). A key this file does not
 * know is shown by its own name, never dropped, so a rule written by hand or
 * by a newer server still reads as something.
 */

import type { StageOut } from "@/types/api";

/** A piece of a sentence; `strong` marks what the rule is about. */
export interface Part {
  text: string;
  strong?: boolean;
}

export interface RuleSummary {
  /** The condition, e.g. "sender is **a@b.c** and subject contains **sale**". */
  when: Part[];
  /** What happens, e.g. "move to **Newsletter**, mark read". */
  then: Part[];
  /** A matching mail ends the list for it. */
  stop: boolean;
  /** The rule removes mail from the server for good. */
  permanent: boolean;
}

export type RuleGroupId = "spam" | "filing" | "forwarding" | "orders" | "other";

export const RULE_GROUPS: { id: RuleGroupId; label: string }[] = [
  { id: "spam", label: "Spam" },
  { id: "filing", label: "Filing" },
  { id: "forwarding", label: "Forwarding" },
  { id: "orders", label: "Orders & tickets" },
  { id: "other", label: "Other" },
];

/** Looks up a folder's display name by id; undefined when unknown. */
export type FolderNameLookup = (folderId: string) => string | undefined;

type Json = Record<string, unknown>;

const MAX_WHEN_CHARS = 90;
const SPECIAL_USE_LABEL: Record<string, string> = {
  inbox: "Inbox",
  junk: "Junk",
  trash: "Trash",
  archive: "Archive",
  sent: "Sent",
  drafts: "Drafts",
};

export const CLASSIFY_SENTENCE =
  "Reads every incoming mail with the administrator's model and decides whether it is spam";
export const ORDERS_SENTENCE =
  "Bundles orders and tickets found in mail into the Orders list, for accounts with that switched on";

function isObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function plain(parts: Part[]): string {
  return parts.map((p) => p.text).join("");
}

function t(text: string): Part {
  return { text };
}
function s(text: string): Part {
  return { text, strong: true };
}

function joinParts(groups: Part[][], separator: string): Part[] {
  const out: Part[] = [];
  groups.forEach((group, i) => {
    if (i > 0) out.push(t(separator));
    out.push(...group);
  });
  return out;
}

function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) {
    const mb = bytes / (1024 * 1024);
    return `${Number.isInteger(mb) ? mb : mb.toFixed(1)} MB`;
  }
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

function leafCondition(key: string, value: unknown): Part[] {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  switch (key) {
    case "sender_match":
      return text.includes("@")
        ? [t("sender is "), s(text)]
        : [t("sender domain "), s(text)];
    case "sender_domain":
      return [t("sender domain "), s(text)];
    case "subject_contains":
      return [t("subject contains "), s(text)];
    case "subject_matches":
      return [t("subject matches "), s(`/${text}/`)];
    case "body_contains":
      return [t("body contains "), s(text)];
    case "body_matches":
      return [t("body matches "), s(`/${text}/`)];
    case "header_match": {
      const field = isObject(value) ? String(value.field ?? "") : "";
      const pattern = isObject(value) ? String(value.pattern ?? "") : "";
      return [t("header "), s(field), t(" matches "), s(`/${pattern}/`)];
    }
    case "header_exists":
      return [t("header "), s(text), t(" exists")];
    case "size_gt":
      return [t("larger than "), s(formatBytes(Number(value)))];
    case "size_lt":
      return [t("smaller than "), s(formatBytes(Number(value)))];
    case "has_attachment":
      return typeof value === "string" && value
        ? [t("has a "), s(value), t(" attachment")]
        : [t("has an attachment")];
    case "folder_is":
      return [t("in "), s(text)];
    case "tag_is":
      return [t("tagged "), s(text)];
    case "verdict_is":
      return [t("classified as "), s(value === "spam" ? "spam" : "not spam")];
    default:
      return [s(key)];
  }
}

/** Number of leaf conditions under a tree. */
function leafCount(cond: unknown): number {
  if (!isObject(cond)) return 0;
  if (Array.isArray(cond.all)) return cond.all.reduce<number>((n, c) => n + leafCount(c), 0);
  if (Array.isArray(cond.any)) return cond.any.reduce<number>((n, c) => n + leafCount(c), 0);
  if ("not" in cond) return leafCount(cond.not);
  return Object.keys(cond).length === 0 ? 0 : 1;
}

function describeCondition(cond: unknown): Part[] {
  if (!isObject(cond) || Object.keys(cond).length === 0) return [t("every mail")];
  if (Array.isArray(cond.all)) {
    if (cond.all.length === 0) return [t("every mail")];
    return joinParts(cond.all.map(describeCondition), " and ");
  }
  if (Array.isArray(cond.any)) {
    const items = cond.any.map(describeCondition);
    const shown = items.slice(0, 2);
    const rest = items.length - shown.length;
    const body = joinParts(shown, ", ");
    return [t("any of: "), ...body, ...(rest > 0 ? [t(` (+${rest})`)] : [])];
  }
  if ("not" in cond) return [t("not "), ...describeCondition(cond.not)];
  const [key] = Object.keys(cond);
  return leafCondition(key, cond[key]);
}

/** The condition sentence, collapsed to a count when it would not fit a line. */
function describeWhen(cond: unknown): Part[] {
  const parts = describeCondition(cond);
  if (plain(parts).length <= MAX_WHEN_CHARS) return parts;
  const n = leafCount(cond);
  const anyTop = isObject(cond) && Array.isArray(cond.any);
  return [t(anyTop ? `any of ${n} conditions` : `${n} conditions`)];
}

function describeMove(value: Json, folderName: FolderNameLookup): Part[] {
  const id = typeof value.folder_id === "string" ? value.folder_id : null;
  const name =
    (id ? folderName(id) : undefined) ??
    (typeof value.folder_name === "string" && value.folder_name ? value.folder_name : undefined) ??
    (typeof value.special_use === "string" ? SPECIAL_USE_LABEL[value.special_use] : undefined) ??
    (typeof value.special_use === "string" ? value.special_use : undefined);
  return name ? [t("move to "), s(name)] : [t("move to a folder")];
}

function describeEffect(effect: unknown, folderName: FolderNameLookup): Part[][] {
  if (!isObject(effect)) return [];
  const [kind] = Object.keys(effect);
  if (kind === undefined) return [];
  const raw = effect[kind];
  const value: Json = isObject(raw) ? raw : {};
  switch (kind) {
    case "move":
      return [describeMove(value, folderName)];
    case "trash":
      return [[t("move to trash")]];
    case "expunge":
      return [[s("delete permanently")]];
    case "set_flags": {
      const out: Part[][] = [];
      if (value.seen === true) out.push([t("mark read")]);
      if (value.seen === false) out.push([t("mark unread")]);
      if (value.flagged === true) out.push([t("star")]);
      if (value.flagged === false) out.push([t("remove star")]);
      if (value.answered === true) out.push([t("mark answered")]);
      if (value.deleted === true) out.push([t("flag as deleted")]);
      return out;
    }
    case "keywords":
    case "tag": {
      const noun = kind === "tag" ? "tag" : "keyword";
      const add = Array.isArray(value.add) ? value.add.map(String) : [];
      const remove = Array.isArray(value.remove) ? value.remove.map(String) : [];
      return [
        ...add.map((x) => [t(`add ${noun} `), s(x)]),
        ...remove.map((x) => [t(`remove ${noun} `), s(x)]),
      ];
    }
    case "notify":
      return [[t("notify")]];
    case "webhook":
      return [[t("send to "), s(String(value.name ?? "a webhook"))]];
    case "record_verdict":
      return [[t("record verdict")]];
    case "enqueue_order":
      return [[t("queue for orders")]];
    default:
      return [[s(kind)]];
  }
}

function effectsOf(stage: StageOut): unknown[] {
  const effects = stage.config.effects;
  return Array.isArray(effects) ? effects : [];
}

function hasEffect(stage: StageOut, kind: string): boolean {
  return effectsOf(stage).some((e) => isObject(e) && kind in e);
}

/** Whether a condition tree anywhere uses the given leaf key. */
export function usesCondition(cond: unknown, key: string): boolean {
  if (!isObject(cond)) return false;
  if (Array.isArray(cond.all)) return cond.all.some((c) => usesCondition(c, key));
  if (Array.isArray(cond.any)) return cond.any.some((c) => usesCondition(c, key));
  if ("not" in cond) return usesCondition(cond.not, key);
  return key in cond;
}

export function describeRule(stage: StageOut, folderName: FolderNameLookup = () => undefined): RuleSummary {
  if (stage.type === "classify") {
    return { when: [t(CLASSIFY_SENTENCE)], then: [], stop: stage.halt, permanent: false };
  }
  if (stage.type === "orders") {
    return { when: [t(ORDERS_SENTENCE)], then: [], stop: stage.halt, permanent: false };
  }
  if (stage.type !== "match") {
    return { when: [t(`A “${stage.type}” step`)], then: [], stop: stage.halt, permanent: false };
  }
  const effects = effectsOf(stage).flatMap((e) => describeEffect(e, folderName));
  return {
    when: describeWhen(stage.config.when),
    then: effects.length > 0 ? joinParts(effects, ", ") : [t("do nothing")],
    stop: stage.halt,
    permanent: hasEffect(stage, "expunge"),
  };
}

/** The rule as plain text, for filtering and accessible names. */
export function summaryText(summary: RuleSummary): string {
  const when = plain(summary.when);
  const then = plain(summary.then);
  return then ? `${when} → ${then}` : when;
}

export function ruleGroup(stage: StageOut): RuleGroupId {
  if (stage.type === "classify") return "spam";
  if (stage.type === "orders") return "orders";
  if (stage.type !== "match") return "other";
  if (hasEffect(stage, "webhook")) return "forwarding";
  if (usesCondition(stage.config.when, "verdict_is")) return "spam";
  return "filing";
}

/** A stage the form editor does not cover; it opens in JSON, never in a form. */
export function isFixedStage(stage: StageOut): boolean {
  return stage.type === "classify" || stage.type === "orders";
}
