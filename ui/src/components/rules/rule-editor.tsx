"use client";

/**
 * Create or edit one rule: a form of conditions and actions, with the raw
 * JSON one switch away. The form opens only for rules it can show exactly;
 * anything else (a condition it has no row for, a webhook) opens in JSON
 * with a notice, so no edit can quietly lose part of a rule.
 */

import { useMemo, useState } from "react";
import { Loader2, Plus, X } from "lucide-react";

import { ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { OptionSelect, type Option } from "@/components/settings/setting-fields";
import { useAccounts } from "@/hooks/use-accounts";
import { useIsAdmin } from "@/hooks/use-me";
import { useCreateStage, useUpdateStage } from "@/hooks/use-pipeline";
import { useSearchFolders } from "@/hooks/use-search-folders";
import { folderDisplayName } from "@/lib/folders";
import {
  actionError,
  buildEffects,
  buildWhen,
  conditionError,
  emptyAction,
  emptyConditionRow,
  parseEffects,
  parseWhen,
  type ActionKind,
  type ActionRow,
  type Combinator,
  type ConditionGroup,
  type ConditionKind,
  type ConditionRow,
  type SizeUnit,
  type WhenForm,
} from "@/lib/rule-form";
import { isFixedStage } from "@/lib/rule-summary";
import { cn } from "@/lib/utils";
import type { AccountResponse, PipelineDocument, StageOut } from "@/types/api";

const CONDITION_OPTIONS: Option[] = [
  { value: "sender_match", label: "Sender is" },
  { value: "sender_domain", label: "Sender domain is" },
  { value: "subject_contains", label: "Subject contains" },
  { value: "subject_matches", label: "Subject matches (regex)" },
  { value: "body_contains", label: "Body contains" },
  { value: "body_matches", label: "Body matches (regex)" },
  { value: "header_exists", label: "Header exists" },
  { value: "header_match", label: "Header matches (regex)" },
  { value: "attachment_any", label: "Has an attachment" },
  { value: "attachment_type", label: "Has an attachment of type" },
  { value: "size_gt", label: "Larger than" },
  { value: "size_lt", label: "Smaller than" },
  { value: "folder_is", label: "Is in folder" },
  { value: "tag_is", label: "Is tagged" },
  { value: "verdict_spam", label: "Is classified as spam" },
  { value: "verdict_ham", label: "Is classified as not spam" },
];

const VALUE_PLACEHOLDER: Partial<Record<ConditionKind, string>> = {
  sender_match: "name@example.com or example.com",
  sender_domain: "example.com",
  subject_contains: "invoice",
  subject_matches: "^Re: .*report",
  body_contains: "unsubscribe",
  body_matches: "order #\\d+",
  header_exists: "List-Id",
  header_match: "newsletter|digest",
  attachment_type: "pdf",
  folder_is: "INBOX",
  tag_is: "work",
};

const ACTION_OPTIONS: Option[] = [
  { value: "move", label: "Move to folder" },
  { value: "seen", label: "Mark as read" },
  { value: "unseen", label: "Mark as unread" },
  { value: "star", label: "Star" },
  { value: "unstar", label: "Remove star" },
  { value: "keyword", label: "Add keyword" },
  { value: "tag", label: "Add tag" },
  { value: "trash", label: "Move to trash" },
  { value: "expunge", label: "Delete permanently" },
  { value: "notify", label: "Notify me" },
];

const UNIT_OPTIONS: Option[] = [
  { value: "KB", label: "KB" },
  { value: "MB", label: "MB" },
];

const COMBINATOR_OPTIONS: Option[] = [
  { value: "all", label: "All of these" },
  { value: "any", label: "Any of these" },
];

const WEBHOOK_TEMPLATE = {
  webhook: {
    name: "my-webhook",
    url: "https://example.com/endpoint",
    headers: { Authorization: "Bearer {{secret:MY_TOKEN}}" },
    received_at_param: "received_at",
  },
};

/** The name a new rule's id is derived from, unique within the document. */
function newStageId(name: string, taken: string[]): string {
  const base =
    name
      .toLowerCase()
      .normalize("NFKD")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40) || "rule";
  let id = base;
  for (let n = 2; taken.includes(id); n += 1) id = `${base}-${n}`;
  return id;
}

// --- Condition rows -----------------------------------------------------

function ConditionRowEditor({
  row,
  onChange,
  onRemove,
  showErrors,
}: {
  row: ConditionRow;
  onChange: (next: ConditionRow) => void;
  onRemove: () => void;
  showErrors: boolean;
}) {
  const error = showErrors ? conditionError(row) : null;
  const needsValue = !["attachment_any", "verdict_spam", "verdict_ham"].includes(row.kind);
  const isSize = row.kind === "size_gt" || row.kind === "size_lt";
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2">
        <OptionSelect
          aria-label="Condition"
          value={row.kind}
          options={CONDITION_OPTIONS}
          className="order-1 w-[calc(100%-2.25rem)] sm:w-56"
          onChange={(v) => onChange({ ...row, kind: v as ConditionKind })}
        />
        {row.kind === "header_match" && (
          <Input
            aria-label="Header name"
            placeholder="From"
            value={row.field}
            className="order-3 w-32"
            onChange={(e) => onChange({ ...row, field: e.target.value })}
          />
        )}
        {needsValue && (
          <Input
            aria-label="Value"
            aria-invalid={error ? true : undefined}
            inputMode={isSize ? "decimal" : undefined}
            placeholder={isSize ? "2" : VALUE_PLACEHOLDER[row.kind]}
            value={row.value}
            className="order-3 min-w-40 flex-1"
            onChange={(e) => onChange({ ...row, value: e.target.value })}
          />
        )}
        {isSize && (
          <OptionSelect
            aria-label="Unit"
            value={row.unit}
            options={UNIT_OPTIONS}
            className="order-3 w-20"
            onChange={(v) => onChange({ ...row, unit: v as SizeUnit })}
          />
        )}
        <label className="order-3 flex items-center gap-1.5 text-xs text-muted-foreground">
          <input
            type="checkbox"
            className="h-4 w-4"
            checked={row.negate}
            onChange={(e) => onChange({ ...row, negate: e.target.checked })}
          />
          not
        </label>
        <Button
          variant="ghost"
          size="icon-sm"
          className="order-2 sm:order-4"
          aria-label="Remove condition"
          onClick={onRemove}
        >
          <X className="h-4 w-4" />
        </Button>
      </div>
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}

function GroupEditor({
  group,
  onChange,
  onRemove,
  showErrors,
}: {
  group: ConditionGroup;
  onChange: (next: ConditionGroup) => void;
  onRemove: () => void;
  showErrors: boolean;
}) {
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-dashed p-3">
      <div className="flex items-center justify-between gap-2">
        <OptionSelect
          aria-label="Combine this group"
          value={group.combinator}
          options={COMBINATOR_OPTIONS}
          className="w-36"
          onChange={(v) => onChange({ ...group, combinator: v as Combinator })}
        />
        <Button variant="ghost" size="sm" onClick={onRemove}>
          Remove group
        </Button>
      </div>
      {group.rows.map((row) => (
        <ConditionRowEditor
          key={row.id}
          row={row}
          showErrors={showErrors}
          onChange={(next) =>
            onChange({ ...group, rows: group.rows.map((r) => (r.id === row.id ? next : r)) })
          }
          onRemove={() => onChange({ ...group, rows: group.rows.filter((r) => r.id !== row.id) })}
        />
      ))}
      <div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => onChange({ ...group, rows: [...group.rows, emptyConditionRow()] })}
        >
          <Plus className="mr-1 h-3.5 w-3.5" />
          Add condition
        </Button>
      </div>
    </div>
  );
}

// --- Action rows --------------------------------------------------------

interface FolderChoices {
  options: Option[];
}

function ActionRowEditor({
  row,
  folders,
  onChange,
  onRemove,
  showErrors,
}: {
  row: ActionRow;
  folders: FolderChoices;
  onChange: (next: ActionRow) => void;
  onRemove: () => void;
  showErrors: boolean;
}) {
  const error = showErrors ? actionError(row) : null;
  const textual = row.kind === "keyword" || row.kind === "tag" || row.kind === "notify";
  const moveOptions = useMemo(() => {
    if (row.kind !== "move" || !row.value || folders.options.some((o) => o.value === row.value)) {
      return folders.options;
    }
    return [...folders.options, { value: row.value, label: row.value.slice(row.value.indexOf(":") + 1) }];
  }, [folders.options, row.kind, row.value]);
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2">
        <OptionSelect
          aria-label="Action"
          value={row.kind}
          options={ACTION_OPTIONS}
          className={cn(
            "order-1 w-[calc(100%-2.25rem)] sm:w-48",
            row.kind === "expunge" && "text-destructive",
          )}
          onChange={(v) => onChange({ ...row, kind: v as ActionKind, value: "" })}
        />
        {row.kind === "move" && (
          <OptionSelect
            aria-label="Folder"
            value={row.value}
            options={moveOptions}
            placeholder="Choose a folder"
            className="order-3 min-w-40 flex-1"
            onChange={(v) => onChange({ ...row, value: String(v) })}
          />
        )}
        {textual && (
          <Input
            aria-label="Value"
            aria-invalid={error ? true : undefined}
            placeholder={row.kind === "notify" ? "Message" : row.kind === "tag" ? "tag" : "keyword"}
            value={row.value}
            className="order-3 min-w-40 flex-1"
            onChange={(e) => onChange({ ...row, value: e.target.value })}
          />
        )}
        <Button
          variant="ghost"
          size="icon-sm"
          className="order-2 sm:order-4"
          aria-label="Remove action"
          onClick={onRemove}
        >
          <X className="h-4 w-4" />
        </Button>
      </div>
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}

/** Folder choices for a move: roles that mean the same in every account,
 * then the in-scope accounts' own folders. One account allows an exact
 * folder; several can only share a name. */
function useFolderChoices(scopeAccountIds: string[] | null): FolderChoices {
  const { options: all } = useSearchFolders();
  return useMemo(() => {
    const inScope = all.filter(
      (o) => o.folder.kind !== "glacier" && (scopeAccountIds === null || scopeAccountIds.includes(o.accountId)),
    );
    const accountIds = new Set(inScope.map((o) => o.accountId));
    const out: Option[] = [];
    for (const [use, label] of [
      ["junk", "Junk"],
      ["archive", "Archive"],
      ["inbox", "Inbox"],
    ] as const) {
      if (inScope.some((o) => o.folder.special_use === use)) {
        out.push({ value: `use:${use}`, label: accountIds.size > 1 ? `${label} (each account's own)` : label });
      }
    }
    const seen = new Set<string>();
    for (const o of inScope) {
      if (o.folder.special_use) continue;
      if (accountIds.size === 1) {
        out.push({ value: `id:${o.folder.id}`, label: folderDisplayName(o.folder) });
      } else if (!seen.has(o.folder.imap_name)) {
        seen.add(o.folder.imap_name);
        out.push({ value: `name:${o.folder.imap_name}`, label: folderDisplayName(o.folder) });
      }
    }
    return { options: out };
  }, [all, scopeAccountIds]);
}

// --- Applies to ---------------------------------------------------------

function AppliesTo({
  accounts,
  all,
  selected,
  onChange,
}: {
  accounts: AccountResponse[];
  all: boolean;
  selected: string[];
  onChange: (all: boolean, selected: string[]) => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      <Label className="text-sm font-medium">Applies to</Label>
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          size="sm"
          variant={all ? "default" : "outline"}
          aria-pressed={all}
          onClick={() => onChange(true, selected)}
        >
          All my accounts
        </Button>
        {accounts.map((a) => {
          const on = !all && selected.includes(a.id);
          return (
            <Button
              key={a.id}
              type="button"
              size="sm"
              variant={on ? "default" : "outline"}
              aria-pressed={on}
              onClick={() => {
                const base = all ? [] : selected;
                onChange(false, on ? base.filter((id) => id !== a.id) : [...base, a.id]);
              }}
            >
              {a.emoji ? `${a.emoji} ` : ""}
              {a.name}
            </Button>
          );
        })}
      </div>
    </div>
  );
}

// --- JSON mode ----------------------------------------------------------

function parseError(text: string): string | null {
  try {
    JSON.parse(text);
    return null;
  } catch (err) {
    return err instanceof Error ? err.message : "Not valid JSON";
  }
}

function JsonBox({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
}) {
  const error = parseError(value);
  return (
    <div className="grid gap-1.5">
      <Label className="text-sm font-medium">{label}</Label>
      <Textarea
        value={value}
        rows={8}
        aria-label={label}
        aria-invalid={error ? true : undefined}
        className="font-mono text-xs"
        onChange={(e) => onChange(e.target.value)}
      />
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}

// --- The editor ---------------------------------------------------------

export interface RuleEditorProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  pipeline: PipelineDocument;
  /** The rule being edited; absent when adding. */
  stage?: StageOut;
  /** A rule to start a new one from (Duplicate). */
  template?: StageOut;
}

export function RuleEditor(props: RuleEditorProps) {
  // The body is keyed on what was opened, so each opening starts from the
  // rule itself rather than from the previous session's edits.
  const key = props.open ? `${props.stage?.stage_id ?? props.template?.stage_id ?? "new"}` : "closed";
  return (
    <Dialog open={props.open} onOpenChange={props.onOpenChange}>
      <DialogContent size="xl" className="max-h-[90dvh] overflow-y-auto">
        {props.open && <EditorBody key={key} {...props} />}
      </DialogContent>
    </Dialog>
  );
}

function EditorBody({ onOpenChange, pipeline, stage, template }: RuleEditorProps) {
  const source = stage ?? template;
  const editing = !!stage;
  const fixed = !!stage && isFixedStage(stage);
  const isAdmin = useIsAdmin();
  const { data: accounts } = useAccounts();
  const createStage = useCreateStage();
  const updateStage = useUpdateStage();

  const initialWhen = useMemo(() => (source ? parseWhen(source.config.when) : parseWhen({})), [source]);
  const initialActions = useMemo(
    () => (source ? parseEffects(source.config.effects) : []),
    [source],
  );
  const representable = initialWhen !== null && initialActions !== null;

  const [name, setName] = useState(
    stage?.name ?? (template ? `${template.name} (copy)` : ""),
  );
  const [halt, setHalt] = useState(source?.halt ?? false);
  const [scopeAll, setScopeAll] = useState(source ? source.accounts === null : true);
  const [selected, setSelected] = useState<string[]>(source?.accounts ?? []);
  // A brand-new rule starts with one empty row of each, so the form is
  // never a blank page.
  const [when, setWhen] = useState<WhenForm>(() =>
    source
      ? (initialWhen ?? { combinator: "all", items: [] })
      : { combinator: "all", items: [{ type: "row", row: emptyConditionRow() }] },
  );
  const [actions, setActions] = useState<ActionRow[]>(() =>
    source ? (initialActions ?? []) : [emptyAction()],
  );
  const [mode, setMode] = useState<"form" | "json">(representable ? "form" : "json");
  const [notice, setNotice] = useState<string | null>(
    representable ? null : "This rule uses conditions or actions the form cannot show, so it opens as JSON.",
  );
  const [whenText, setWhenText] = useState(() =>
    JSON.stringify(source?.config.when ?? {}, null, 2),
  );
  const [effectsText, setEffectsText] = useState(() =>
    JSON.stringify(source?.config.effects ?? [], null, 2),
  );
  const [showErrors, setShowErrors] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmPermanent, setConfirmPermanent] = useState(false);

  const accountList = accounts ?? [];
  const scopeIds = scopeAll ? null : selected;
  const folders = useFolderChoices(scopeIds);
  const pending = createStage.isPending || updateStage.isPending;

  const rowsOf = (w: WhenForm): ConditionRow[] =>
    w.items.flatMap((i) => (i.type === "row" ? [i.row] : i.group.rows));

  const switchToJson = () => {
    setWhenText(JSON.stringify(buildWhen(when), null, 2));
    setEffectsText(JSON.stringify(buildEffects(actions), null, 2));
    setNotice(null);
    setMode("json");
  };

  const switchToForm = () => {
    let whenJson: unknown;
    let effectsJson: unknown;
    try {
      whenJson = JSON.parse(whenText);
      effectsJson = JSON.parse(effectsText);
    } catch {
      setNotice("Fix the JSON before switching back to the form.");
      return;
    }
    const nextWhen = parseWhen(whenJson);
    const nextActions = parseEffects(effectsJson);
    if (nextWhen === null || nextActions === null) {
      setNotice("The form cannot show this rule exactly, so it stays as JSON.");
      return;
    }
    setWhen(nextWhen);
    setActions(nextActions);
    setNotice(null);
    setMode("form");
  };

  /** The config to save, or null with the problem shown. */
  const buildConfig = (): Record<string, unknown> | null => {
    if (fixed) return stage!.config;
    if (mode === "json") {
      let whenJson: unknown;
      let effectsJson: unknown;
      try {
        whenJson = JSON.parse(whenText);
        effectsJson = JSON.parse(effectsText);
      } catch {
        setError("Fix the JSON first: it does not parse.");
        return null;
      }
      return { ...(source?.config ?? {}), when: whenJson, effects: effectsJson };
    }
    setShowErrors(true);
    if (actions.length === 0) {
      setError("Add at least one action.");
      return null;
    }
    if (rowsOf(when).some((r) => conditionError(r)) || actions.some((a) => actionError(a))) {
      setError("Fill in the highlighted fields.");
      return null;
    }
    return { ...(source?.config ?? {}), when: buildWhen(when), effects: buildEffects(actions) };
  };

  const hasPermanent = (config: Record<string, unknown>): boolean =>
    Array.isArray(config.effects) &&
    config.effects.some((e) => typeof e === "object" && e !== null && "expunge" in e);

  const save = (config: Record<string, unknown>) => {
    setError(null);
    const accountsPayload = scopeAll ? null : selected;
    const onError = (err: unknown) => setError(err instanceof ApiError ? err.message : String(err));
    if (editing) {
      updateStage.mutate(
        {
          stageId: stage!.stage_id,
          data: {
            name: name.trim() || null,
            config,
            halt,
            accounts: accountsPayload,
            base_revision: pipeline.revision,
          },
        },
        { onSuccess: () => onOpenChange(false), onError },
      );
    } else {
      createStage.mutate(
        {
          stage_id: newStageId(name, pipeline.stages.map((s) => s.stage_id)),
          type: "match",
          name: name.trim() || null,
          config,
          enabled: true,
          halt,
          accounts: accountsPayload,
          base_revision: pipeline.revision,
        },
        { onSuccess: () => onOpenChange(false), onError },
      );
    }
  };

  const submit = () => {
    if (pending) return;
    if (!fixed && !name.trim()) {
      setError("Give the rule a name.");
      setShowErrors(true);
      return;
    }
    if (!scopeAll && selected.length === 0) {
      setError("Choose at least one account, or all of them.");
      return;
    }
    const config = buildConfig();
    if (config === null) return;
    if (!fixed && hasPermanent(config)) {
      setConfirmPermanent(true);
      return;
    }
    save(config);
  };

  return (
    <div
      className="grid gap-4"
      onKeyDown={(e) => {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
          e.preventDefault();
          submit();
        }
      }}
    >
      <DialogHeader>
        <DialogTitle>
          {editing ? (fixed ? `Accounts for “${stage!.name}”` : "Edit rule") : "Add rule"}
        </DialogTitle>
      </DialogHeader>

      {!fixed && (
        <div className="grid gap-1.5">
          <Label htmlFor="rule-name" className="text-sm font-medium">
            Name
          </Label>
          <Input
            id="rule-name"
            value={name}
            aria-invalid={showErrors && !name.trim() ? true : undefined}
            placeholder="Newsletters to their folder"
            onChange={(e) => setName(e.target.value)}
          />
        </div>
      )}

      {fixed && (
        <p className="text-sm text-muted-foreground">
          This step has no conditions of its own; choose which of your accounts it works on.
        </p>
      )}

      {!fixed && notice && (
        <p role="status" className="rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          {notice}
        </p>
      )}

      {!fixed && mode === "form" && (
        <>
          <section className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-medium">When a mail arrives and</h3>
              {when.items.length > 1 && (
                <OptionSelect
                  aria-label="Combine conditions"
                  value={when.combinator}
                  options={COMBINATOR_OPTIONS}
                  className="w-36"
                  onChange={(v) => setWhen({ ...when, combinator: v as Combinator })}
                />
              )}
            </div>
            {when.items.length === 0 && (
              <p className="text-xs text-muted-foreground">No conditions: the rule applies to every mail.</p>
            )}
            {when.items.map((item) =>
              item.type === "row" ? (
                <ConditionRowEditor
                  key={item.row.id}
                  row={item.row}
                  showErrors={showErrors}
                  onChange={(next) =>
                    setWhen({
                      ...when,
                      items: when.items.map((i) =>
                        i.type === "row" && i.row.id === item.row.id ? { type: "row", row: next } : i,
                      ),
                    })
                  }
                  onRemove={() =>
                    setWhen({
                      ...when,
                      items: when.items.filter((i) => !(i.type === "row" && i.row.id === item.row.id)),
                    })
                  }
                />
              ) : (
                <GroupEditor
                  key={item.group.rows[0]?.id ?? "group"}
                  group={item.group}
                  showErrors={showErrors}
                  onChange={(next) =>
                    setWhen({
                      ...when,
                      items: when.items.map((i) => (i === item ? { type: "group", group: next } : i)),
                    })
                  }
                  onRemove={() => setWhen({ ...when, items: when.items.filter((i) => i !== item) })}
                />
              ),
            )}
            <div className="flex flex-wrap gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() =>
                  setWhen({ ...when, items: [...when.items, { type: "row", row: emptyConditionRow() }] })
                }
              >
                <Plus className="mr-1 h-3.5 w-3.5" />
                Add condition
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() =>
                  setWhen({
                    ...when,
                    items: [
                      ...when.items,
                      {
                        type: "group",
                        group: { combinator: "any", rows: [emptyConditionRow(), emptyConditionRow()] },
                      },
                    ],
                  })
                }
              >
                <Plus className="mr-1 h-3.5 w-3.5" />
                Add group
              </Button>
            </div>
          </section>

          <section className="flex flex-col gap-2">
            <h3 className="text-sm font-medium">Then</h3>
            {actions.map((row) => (
              <ActionRowEditor
                key={row.id}
                row={row}
                folders={folders}
                showErrors={showErrors}
                onChange={(next) => setActions(actions.map((a) => (a.id === row.id ? next : a)))}
                onRemove={() => setActions(actions.filter((a) => a.id !== row.id))}
              />
            ))}
            <div>
              <Button variant="outline" size="sm" onClick={() => setActions([...actions, emptyAction()])}>
                <Plus className="mr-1 h-3.5 w-3.5" />
                Add action
              </Button>
            </div>
          </section>
        </>
      )}

      {!fixed && mode === "json" && (
        <>
          <JsonBox label="Conditions (JSON)" value={whenText} onChange={setWhenText} />
          <JsonBox label="Actions (JSON)" value={effectsText} onChange={setEffectsText} />
          {isAdmin && (
            <div className="flex flex-wrap items-center gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => {
                  try {
                    const current = JSON.parse(effectsText);
                    setEffectsText(
                      JSON.stringify([...(Array.isArray(current) ? current : []), WEBHOOK_TEMPLATE], null, 2),
                    );
                  } catch {
                    setNotice("Fix the actions JSON before adding a webhook.");
                  }
                }}
              >
                Add webhook action
              </Button>
              <span className="text-xs text-muted-foreground">
                Sends the matching mail&apos;s raw source to a URL; a header can use a stored secret as{" "}
                {"{{secret:NAME}}"}.
              </span>
            </div>
          )}
        </>
      )}

      <AppliesTo
        accounts={accountList}
        all={scopeAll}
        selected={selected}
        onChange={(all, ids) => {
          setScopeAll(all);
          setSelected(ids);
        }}
      />

      {!fixed && (
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-0.5 h-4 w-4"
            checked={halt}
            onChange={(e) => setHalt(e.target.checked)}
          />
          <span>
            Stop after this rule
            <span className="block text-xs text-muted-foreground">
              A mail it matches goes through no rule below it.
            </span>
          </span>
        </label>
      )}

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2">
        {!fixed ? (
          <Button variant="ghost" size="sm" onClick={mode === "form" ? switchToJson : switchToForm}>
            {mode === "form" ? "Edit as JSON" : "Edit as form"}
          </Button>
        ) : (
          <span />
        )}
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={pending}>
            {pending && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
            Save
          </Button>
        </div>
      </div>

      <ConfirmDialog
        open={confirmPermanent}
        onOpenChange={setConfirmPermanent}
        title="Delete matching mail for good?"
        description="Mail matching this rule will be removed from the server for good. It does not go to the Trash and cannot be brought back."
        confirmLabel="Save rule"
        onConfirm={() => {
          setConfirmPermanent(false);
          const config = buildConfig();
          if (config) save(config);
        }}
      />
    </div>
  );
}
