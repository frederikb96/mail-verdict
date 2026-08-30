"use client";

import { useState } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { JsonSchema, JsonSchemaProperty } from "@/types/api";

/** The scalar JSON Schema types a plain input can represent. Everything
 * else (object, array, anyOf, unset) falls back to a JSON textarea -- the
 * config-editing approach the design explicitly sanctions, and the only
 * one that works for an arbitrary condition tree without hardcoding it. */
function isScalar(prop: JsonSchemaProperty): "boolean" | "string" | "number" | null {
  const type = prop.type ?? prop.anyOf?.find((p) => p.type && p.type !== "null")?.type;
  if (type === "boolean") return "boolean";
  if (type === "integer" || type === "number") return "number";
  if (type === "string") return "string";
  return null;
}

/** A sensible empty value for a non-scalar property, so a fresh "when" or
 * "effects" field starts as `{}` / `[]` instead of a bare "null" the user
 * has to know to replace. */
function defaultForProp(prop: JsonSchemaProperty): unknown {
  const type = prop.type ?? prop.anyOf?.find((p) => p.type && p.type !== "null")?.type;
  if (type === "array") return [];
  if (type === "object") return {};
  return null;
}

/** A fresh config object for a stage type: every object/array property
 * pre-filled with its empty value, so a new stage's JSON fields start as
 * `{}` / `[]` instead of a bare "null" the user has to know to replace. */
export function seedConfigDefaults(schema: JsonSchema): Record<string, unknown> {
  const properties = schema.properties ?? {};
  const seeded: Record<string, unknown> = {};
  for (const [name, prop] of Object.entries(properties)) {
    if (isScalar(prop) === null) {
      seeded[name] = defaultForProp(prop);
    }
  }
  return seeded;
}

function JsonField({
  name,
  label,
  value,
  onChange,
}: {
  name: string;
  label: string;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const [text, setText] = useState(() => JSON.stringify(value ?? null, null, 2));
  const [error, setError] = useState<string | null>(null);

  return (
    <div className="grid gap-1.5">
      <Label className="text-sm" htmlFor={`stage-cfg-${name}`}>
        {label}
      </Label>
      <Textarea
        id={`stage-cfg-${name}`}
        value={text}
        rows={6}
        className="font-mono text-xs"
        onChange={(e) => {
          setText(e.target.value);
          try {
            onChange(JSON.parse(e.target.value));
            setError(null);
          } catch {
            setError("Invalid JSON -- not saved until this parses");
          }
        }}
      />
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}

/**
 * Renders one input per property of a stage type's config JSON Schema.
 * Scalars get a real input; anything with structure (an object or array,
 * which is exactly what a condition tree or effect list is) gets a JSON
 * textarea. Driven entirely by the schema, so a stage type registered
 * later works here without a UI change.
 */
export function StageConfigForm({
  schema,
  value,
  onChange,
}: {
  schema: JsonSchema;
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
}) {
  const properties = schema.properties ?? {};
  const entries = Object.entries(properties);

  const setField = (name: string, fieldValue: unknown) => {
    onChange({ ...value, [name]: fieldValue });
  };

  if (entries.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        This stage type takes no configuration.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {entries.map(([name, prop]) => {
        const current = value[name];
        const label = prop.title ?? name;
        const scalar = isScalar(prop);

        if (scalar === "boolean") {
          return (
            <div key={name} className="flex items-center justify-between">
              <div>
                <Label className="text-sm">{label}</Label>
                {prop.description && (
                  <p className="text-xs text-muted-foreground">{prop.description}</p>
                )}
              </div>
              <input
                type="checkbox"
                checked={Boolean(current)}
                onChange={(e) => setField(name, e.target.checked)}
                className="h-4 w-4"
              />
            </div>
          );
        }

        if (scalar === "number") {
          return (
            <div key={name} className="grid gap-1.5">
              <Label className="text-sm">{label}</Label>
              {prop.description && (
                <p className="text-xs text-muted-foreground">{prop.description}</p>
              )}
              <Input
                type="number"
                value={typeof current === "number" ? current : ""}
                onChange={(e) => setField(name, Number(e.target.value))}
              />
            </div>
          );
        }

        if (scalar === "string" && prop.enum) {
          return (
            <div key={name} className="grid gap-1.5">
              <Label className="text-sm">{label}</Label>
              <Select
                value={typeof current === "string" ? current : undefined}
                onValueChange={(v) => setField(name, v)}
              >
                <SelectTrigger>
                  <SelectValue placeholder="Select…" />
                </SelectTrigger>
                <SelectContent>
                  {prop.enum.map((opt) => (
                    <SelectItem key={opt} value={opt}>
                      {opt}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          );
        }

        if (scalar === "string") {
          return (
            <div key={name} className="grid gap-1.5">
              <Label className="text-sm">{label}</Label>
              {prop.description && (
                <p className="text-xs text-muted-foreground">{prop.description}</p>
              )}
              <Input
                value={typeof current === "string" ? current : ""}
                onChange={(e) => setField(name, e.target.value)}
              />
            </div>
          );
        }

        return (
          <JsonField
            key={name}
            name={name}
            label={prop.description ? `${label} — ${prop.description}` : label}
            value={current}
            onChange={(v) => setField(name, v)}
          />
        );
      })}
    </div>
  );
}
