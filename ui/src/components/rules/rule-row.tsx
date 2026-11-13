"use client";

/**
 * One rule as a row: its name, a sentence saying what it does, where it
 * applies, and its switch and menu. Names and sentences wrap rather than
 * truncate, so nothing a rule says is hidden on a phone; on a narrow screen
 * the scope, switch and menu drop to a line of their own.
 */

import type { ReactNode } from "react";
import { AlertTriangle, GripVertical, MoreVertical } from "lucide-react";
import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Switch } from "@/components/ui/switch";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Part, RuleSummary } from "@/lib/rule-summary";
import { cn } from "@/lib/utils";
import type { StageOut } from "@/types/api";

export interface RuleWarning {
  account: string;
  reference: string;
  detail: string | null;
}

export interface RuleRowActions {
  onToggle: (enabled: boolean) => void;
  onEdit: () => void;
  onDuplicate?: () => void;
  onMove: (direction: -1 | 1) => void;
  canMoveUp: boolean;
  canMoveDown: boolean;
  onTest: () => void;
  onDelete?: () => void;
}

function Sentence({ parts }: { parts: Part[] }) {
  return (
    <>
      {parts.map((p, i) =>
        p.strong ? (
          <strong key={i} className="font-medium text-foreground">
            {p.text}
          </strong>
        ) : (
          <span key={i}>{p.text}</span>
        ),
      )}
    </>
  );
}

/** "If <condition> → <actions>", or the fixed sentence of a built-in step. */
export function RuleSentence({ summary }: { summary: RuleSummary }) {
  if (summary.then.length === 0) return <Sentence parts={summary.when} />;
  return (
    <>
      <span>If </span>
      <Sentence parts={summary.when} />
      <span className="text-muted-foreground/70"> → </span>
      <Sentence parts={summary.then} />
    </>
  );
}

export function RuleRowView({
  stage,
  summary,
  scope,
  warnings,
  actions,
  handle,
}: {
  stage: StageOut;
  summary: RuleSummary;
  scope: string;
  warnings: RuleWarning[];
  actions: RuleRowActions;
  /** The drag handle, when this row can be reordered by dragging. */
  handle?: ReactNode;
}) {
  const muted = !stage.enabled;
  return (
    <div
      data-testid="rule-row"
      data-stage-id={stage.stage_id}
      className="grid grid-cols-[1fr_auto] items-start gap-x-2 gap-y-2 px-3 py-3 sm:grid-cols-[auto_1fr_auto]"
    >
      <div className="hidden w-5 pt-0.5 sm:block">{handle}</div>

      <div className="col-span-2 min-w-0 sm:col-span-1">
        <div className={cn("break-words text-sm font-medium", muted && "text-muted-foreground line-through")}>
          {stage.name}
        </div>
        <p className={cn("mt-0.5 break-words text-sm text-muted-foreground", muted && "opacity-70")}>
          <RuleSentence summary={summary} />
        </p>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <span className="hidden text-xs text-muted-foreground sm:inline">{scope}</span>
          {summary.stop && (
            <Tooltip>
              <TooltipTrigger render={<Badge variant="secondary" className="cursor-help" />}>
                stop
              </TooltipTrigger>
              <TooltipContent side="top">
                A mail this rule matches goes through no rule after it
              </TooltipContent>
            </Tooltip>
          )}
          {summary.permanent && (
            <Badge variant="destructive" className="gap-1">
              <AlertTriangle className="h-3 w-3" />
              permanent
            </Badge>
          )}
        </div>
        {warnings.map((w, i) => (
          <div
            key={i}
            className="mt-1.5 flex items-start gap-1.5 rounded-md border border-destructive/40 bg-destructive/5 px-2 py-1 text-xs text-destructive"
          >
            <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
            <span className="min-w-0 break-words">
              {w.account}: “{w.reference}” not found{w.detail ? ` — ${w.detail}` : ""}
            </span>
          </div>
        ))}
      </div>

      <div className="col-span-2 flex items-center justify-between gap-2 sm:col-span-1 sm:justify-end">
        <span className="min-w-0 break-words text-xs text-muted-foreground sm:hidden">{scope}</span>
        <div className="flex shrink-0 items-center gap-1">
          <Switch
            checked={stage.enabled}
            aria-label={`${stage.enabled ? "Turn off" : "Turn on"} ${stage.name}`}
            onCheckedChange={(checked: boolean) => actions.onToggle(checked)}
          />
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8"
                  aria-label={`Actions for ${stage.name}`}
                />
              }
            >
              <MoreVertical className="h-4 w-4" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-44">
              <DropdownMenuItem onClick={actions.onEdit}>Edit</DropdownMenuItem>
              {actions.onDuplicate && (
                <DropdownMenuItem onClick={actions.onDuplicate}>Duplicate</DropdownMenuItem>
              )}
              <DropdownMenuItem disabled={!actions.canMoveUp} onClick={() => actions.onMove(-1)}>
                Move up
              </DropdownMenuItem>
              <DropdownMenuItem disabled={!actions.canMoveDown} onClick={() => actions.onMove(1)}>
                Move down
              </DropdownMenuItem>
              <DropdownMenuItem onClick={actions.onTest}>Test with a mail…</DropdownMenuItem>
              {actions.onDelete && (
                <>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem variant="destructive" onClick={actions.onDelete}>
                    Delete
                  </DropdownMenuItem>
                </>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
    </div>
  );
}

/** A row that reorders by dragging; the handle is left out on touch, where
 * Move up and Move down in the menu do the job. */
export function SortableRuleRow(props: Omit<Parameters<typeof RuleRowView>[0], "handle">) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: props.stage.stage_id,
  });
  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={cn("border-b last:border-b-0", isDragging && "relative z-10 bg-background opacity-60")}
    >
      <RuleRowView
        {...props}
        handle={
          <button
            type="button"
            className="cursor-grab touch-none rounded p-0.5 text-muted-foreground hover:text-foreground"
            aria-label={`Drag ${props.stage.name} to reorder`}
            {...attributes}
            {...listeners}
          >
            <GripVertical className="h-4 w-4" />
          </button>
        }
      />
    </div>
  );
}

/** A row that stays where it is (the fixed first and last steps). */
export function FixedRuleRow(props: Omit<Parameters<typeof RuleRowView>[0], "handle">) {
  return (
    <div className="border-b last:border-b-0">
      <RuleRowView {...props} />
    </div>
  );
}
