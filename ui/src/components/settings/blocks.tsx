"use client";

/**
 * The pieces every settings category is assembled from, so the pages share
 * one rhythm: a category header, cards that always open with a titled
 * header, rows of a label and its control, and one save bar per category.
 */

import type { ReactNode } from "react";
import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

/** The title line of one category's pane, with its actions on the right and
 * the explanation beneath. */
export function CategoryHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between gap-3">
        <h2 className="min-w-0 text-xl font-semibold">{title}</h2>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>
      {description && <p className="max-w-2xl text-sm text-muted-foreground">{description}</p>}
    </div>
  );
}

/** A card whose header is required, so none opens on a blank band. */
export function SectionCard({
  icon: Icon,
  title,
  description,
  action,
  children,
  className,
}: {
  icon?: React.ComponentType<{ className?: string }>;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Card className={className}>
      <CardHeader className="pb-1">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-base">
            {Icon && <Icon className="h-4 w-4 text-muted-foreground" />}
            {title}
          </CardTitle>
          {action}
        </div>
        {description && <p className="text-xs text-muted-foreground">{description}</p>}
      </CardHeader>
      <CardContent className="flex flex-col gap-4">{children}</CardContent>
    </Card>
  );
}

/** A label with its explanation on the left and the control on the right;
 * stacked on a phone. */
export function FieldRow({
  label,
  help,
  htmlFor,
  inline,
  children,
  className,
}: {
  label: ReactNode;
  help?: ReactNode;
  htmlFor?: string;
  /** Keep the control beside the label on a phone too (a switch). */
  inline?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex gap-2 sm:flex-row sm:items-start sm:justify-between sm:gap-6",
        inline ? "flex-row items-start justify-between gap-4" : "flex-col",
        className,
      )}
    >
      <div className="flex min-w-0 flex-col gap-0.5 sm:max-w-[26rem]">
        <label htmlFor={htmlFor} className="text-sm font-medium">
          {label}
        </label>
        {help && <p className="text-xs text-muted-foreground">{help}</p>}
      </div>
      <div className="flex shrink-0 items-center sm:justify-end">{children}</div>
    </div>
  );
}

/** Sticky bar at the bottom of a category: appears only while something is
 * unsaved. */
export function SaveBar({
  dirtyCount,
  saving,
  onSave,
  onDiscard,
}: {
  dirtyCount: number;
  saving: boolean;
  onSave: () => void;
  onDiscard: () => void;
}) {
  if (dirtyCount === 0) return null;
  return (
    <div
      data-testid="settings-save-bar"
      className="sticky bottom-0 z-10 -mx-4 flex items-center justify-end gap-3 border-t bg-background/95 px-4 py-3 backdrop-blur md:mx-0 md:rounded-lg md:border"
    >
      <span className="mr-auto text-sm text-muted-foreground">
        {dirtyCount} unsaved change{dirtyCount === 1 ? "" : "s"}
      </span>
      <Button variant="outline" size="sm" onClick={onDiscard} disabled={saving}>
        Discard
      </Button>
      <Button size="sm" onClick={onSave} disabled={saving}>
        {saving && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
        Save
      </Button>
    </div>
  );
}

/** A transient "saved" / "could not save" note for controls that write as
 * they change. */
export function SavedNote({ state }: { state: "idle" | "saving" | "saved" | "error" }) {
  if (state === "idle") return null;
  return (
    <span
      role="status"
      className={cn(
        "text-xs",
        state === "error" ? "text-destructive" : "text-muted-foreground",
      )}
    >
      {state === "saving" && "Saving…"}
      {state === "saved" && "✓ Saved"}
      {state === "error" && "Could not save — try again"}
    </span>
  );
}
