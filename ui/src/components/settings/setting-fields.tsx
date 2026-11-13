"use client";

/**
 * Settings controls drawn from a declarative list: a boolean is a switch, a
 * closed set of values a select, a plain number a number box. Values read
 * and write through a `SettingsDraft`, so a form's save bar covers them all.
 */

import { useEffect, useState, type ReactNode } from "react";

import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { FieldRow } from "@/components/settings/blocks";
import { humanizeSettingKey } from "@/components/settings/setting-labels";
import { useSettingsSchema } from "@/hooks/use-settings-schema";
import type { SettingsDraft } from "@/hooks/use-settings-draft";
import { useAllSettings } from "@/hooks/use-settings";
import { cn } from "@/lib/utils";

export interface Option {
  value: string | number;
  label: string;
}

export type FieldSpec = {
  category: string;
  key: string;
  label: string;
  help?: ReactNode;
} & (
  | { control: "switch" }
  | { control: "select"; options: Option[] }
  | { control: "number"; min?: number; step?: number; suffix?: string }
  | { control: "text"; placeholder?: string; /** An empty box stores null. */ nullable?: boolean }
  | { control: "json" }
);

/** A field of a known category, for lists that supply the category once. */
export type FieldOf = FieldSpec extends infer F
  ? F extends unknown
    ? Omit<F, "category">
    : never
  : never;

/** A number box that lets the text be empty or half-typed while the draft
 * only ever receives a real number. */
export function NumberInput({
  value,
  onChange,
  min,
  step,
  id,
  className,
  "aria-label": ariaLabel,
}: {
  value: number;
  onChange: (next: number) => void;
  min?: number;
  step?: number;
  id?: string;
  className?: string;
  "aria-label"?: string;
}) {
  const [text, setText] = useState(String(value));

  useEffect(() => {
    // An outside change (discard, save, another field) replaces the text; the
    // user's own typing already equals the value and is left alone.
    setText((current) => (Number(current) === value && current !== "" ? current : String(value)));
  }, [value]);

  return (
    <Input
      id={id}
      type="number"
      inputMode="decimal"
      min={min}
      step={step}
      aria-label={ariaLabel}
      value={text}
      className={cn("w-28", className)}
      onChange={(e) => {
        setText(e.target.value);
        const n = Number(e.target.value);
        if (e.target.value.trim() !== "" && Number.isFinite(n)) onChange(n);
      }}
      onBlur={() => setText(String(value))}
    />
  );
}

/** A select over typed options. A stored value the list does not name
 * (an older setting, a hand-written one) stays selectable as itself. */
export function OptionSelect({
  value,
  options,
  onChange,
  id,
  className,
  placeholder,
  disabled,
  "aria-label": ariaLabel,
}: {
  value: string | number | null | undefined;
  options: Option[];
  onChange: (next: string | number) => void;
  id?: string;
  className?: string;
  placeholder?: string;
  disabled?: boolean;
  "aria-label"?: string;
}) {
  const current = value ?? "";
  const known = options.some((o) => o.value === current);
  const all: Option[] =
    known || current === "" ? options : [...options, { value: current, label: String(current) }];
  return (
    <Select
      value={String(current)}
      disabled={disabled}
      onValueChange={(v) => {
        const picked = all.find((o) => String(o.value) === v);
        if (picked) onChange(picked.value);
      }}
    >
      <SelectTrigger id={id} aria-label={ariaLabel} className={cn("w-40", className)}>
        <SelectValue placeholder={placeholder}>
          {(v: string) => all.find((o) => String(o.value) === v)?.label ?? placeholder ?? v}
        </SelectValue>
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false}>
        {all.map((o) => (
          <SelectItem key={String(o.value)} value={String(o.value)}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function JsonControl({
  value,
  onChange,
  id,
}: {
  value: unknown;
  onChange: (next: unknown) => void;
  id: string;
}) {
  const [text, setText] = useState(() => JSON.stringify(value, null, 2));
  const [error, setError] = useState<string | null>(null);
  const serialized = JSON.stringify(value, null, 2);

  useEffect(() => {
    // Replace the text only when the value changed from outside (discard,
    // save): half-typed JSON that does not parse yet is left in place.
    try {
      if (JSON.stringify(JSON.parse(text), null, 2) === serialized) return;
    } catch {
      if (error) return;
    }
    setText(serialized);
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serialized]);

  return (
    <div className="flex w-full flex-col gap-1">
      <Textarea
        id={id}
        value={text}
        rows={Math.min(24, Math.max(6, text.split("\n").length))}
        spellCheck={false}
        className="font-mono text-xs"
        onChange={(e) => {
          setText(e.target.value);
          try {
            onChange(JSON.parse(e.target.value));
            setError(null);
          } catch (err) {
            setError(err instanceof Error ? err.message : "Not valid JSON");
          }
        }}
      />
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}

function controlId(field: { category: string; key: string }): string {
  return `setting-${field.category}-${field.key}`;
}

/** The fields the server actually returned a value for. */
export function presentFields(draft: SettingsDraft, fields: FieldSpec[]): FieldSpec[] {
  return fields.filter((f) => draft.has(f.category, f.key));
}

export function SettingFields({
  draft,
  fields,
}: {
  draft: SettingsDraft;
  fields: FieldSpec[];
}) {
  return (
    <>
      {presentFields(draft, fields).map((field) => {
        const id = controlId(field);
        const raw = draft.value(field.category, field.key);
        const set = (next: unknown) => draft.set(field.category, field.key, next);
        if (field.control === "json") {
          return (
            <div key={id} className="flex flex-col gap-2">
              <label htmlFor={id} className="text-sm font-medium">
                {field.label}
              </label>
              {field.help && <p className="text-xs text-muted-foreground">{field.help}</p>}
              <JsonControl id={id} value={raw} onChange={set} />
            </div>
          );
        }
        return (
          <FieldRow
            key={id}
            label={field.label}
            help={field.help}
            htmlFor={id}
            inline={field.control === "switch"}
          >
            {field.control === "switch" && (
              <Switch id={id} checked={raw === true} onCheckedChange={(on: boolean) => set(on)} />
            )}
            {field.control === "select" && (
              <OptionSelect
                id={id}
                value={raw as string | number | null | undefined}
                options={field.options}
                onChange={set}
              />
            )}
            {field.control === "number" && (
              <span className="flex items-center gap-2">
                <NumberInput
                  id={id}
                  value={typeof raw === "number" ? raw : 0}
                  min={field.min}
                  step={field.step}
                  onChange={set}
                />
                {field.suffix && (
                  <span className="text-sm text-muted-foreground">{field.suffix}</span>
                )}
              </span>
            )}
            {field.control === "text" && (
              <Input
                id={id}
                value={typeof raw === "string" ? raw : ""}
                placeholder={field.placeholder}
                className="w-56"
                onChange={(e) =>
                  set(field.nullable && e.target.value === "" ? null : e.target.value)
                }
              />
            )}
          </FieldRow>
        );
      })}
    </>
  );
}

/** A control sniffed from a value's type, for keys no declared field names. */
function sniffedField(category: string, key: string, value: unknown): FieldSpec {
  const label = humanizeSettingKey(key);
  if (typeof value === "boolean") return { category, key, label, control: "switch" };
  if (typeof value === "number") return { category, key, label, control: "number" };
  if (typeof value === "object" && value !== null) return { category, key, label, control: "json" };
  return { category, key, label, control: "text" };
}

/** Every key of a category (or the named ones) as plain controls, labelled
 * from `SETTING_LABELS` -- for the categories that are worker mechanics and
 * earn no hand-built form. */
export function CategoryFields({
  draft,
  category,
  keys,
}: {
  draft: SettingsDraft;
  category: string;
  /** Only these keys; every key of the category when omitted. */
  keys?: string[];
}) {
  const { data: all } = useAllSettings();
  const present = Object.keys(all?.[category] ?? {}).filter(
    (key) => !keys || keys.includes(key),
  );
  return (
    <SettingFields
      draft={draft}
      fields={present.map((key) => sniffedField(category, key, all?.[category]?.[key]))}
    />
  );
}

/** Keys of a category the server tags for this area that no declared
 * field names -- a setting added on the server shows up here as a plain
 * control until it earns a proper one. Nothing renders while the server's
 * schema is unavailable. */
export function UndeclaredFields({
  draft,
  category,
  area,
  declared,
  skip = [],
}: {
  draft: SettingsDraft;
  category: string;
  area: "user" | "admin";
  declared: string[];
  skip?: string[];
}) {
  const { data: schema } = useSettingsSchema();
  const { data: all } = useAllSettings();
  const keys = Object.keys(all?.[category] ?? {}).filter(
    (key) =>
      !declared.includes(key) &&
      !skip.includes(key) &&
      schema?.[category]?.[key]?.scope === area,
  );
  return (
    <SettingFields
      draft={draft}
      fields={keys.map((key) => sniffedField(category, key, all?.[category]?.[key]))}
    />
  );
}
