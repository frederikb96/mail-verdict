"use client";

/**
 * Rules: what happens to a user's mail as it arrives. One list per person,
 * grouped by purpose, each rule a sentence with a switch; the assistant and
 * a form editor are the ways to add one, JSON the advanced way.
 */

import { useEffect, useMemo, useState } from "react";
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { History, ListFilter, MoreVertical, PauseCircle, Plus, Sparkles } from "lucide-react";

import { AutomaticFeatures } from "@/components/rules/automatic-features";
import { DescribeRuleDialog } from "@/components/rules/describe-rule-dialog";
import {
  FixedRuleRow,
  SortableRuleRow,
  type RuleRowActions,
  type RuleWarning,
} from "@/components/rules/rule-row";
import { RuleEditor } from "@/components/rules/rule-editor";
import { SectionCard } from "@/components/settings/blocks";
import { PipelineTestDialog } from "@/components/pipeline/pipeline-test-dialog";
import { RevisionsDialog } from "@/components/pipeline/revisions-dialog";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useAccounts } from "@/hooks/use-accounts";
import { useFolderNames } from "@/hooks/use-folder-names";
import {
  usePipeline,
  usePipelineHealth,
  useDeleteStage,
  useReorderStages,
  useSetPipelineEnabled,
  useUpdateStage,
} from "@/hooks/use-pipeline";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api";
import { moveWithinGroup, reorderWithinGroup } from "@/lib/rule-order";
import {
  RULE_GROUPS,
  describeRule,
  isFixedStage,
  ruleGroup,
  summaryText,
  type RuleGroupId,
} from "@/lib/rule-summary";
import type { PipelineDocument, StageOut } from "@/types/api";

type EditorState = { stage?: StageOut; template?: StageOut } | null;

export function RulesPage() {
  const { data: pipeline, isLoading, error } = usePipeline();
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4 md:p-6">
      {isLoading && <Skeleton className="h-64 w-full" />}
      {error && !pipeline && (
        <p role="alert" className="text-sm text-destructive">
          {error instanceof ApiError ? error.message : "Could not load your rules."}
        </p>
      )}
      {pipeline && <RulesBody pipeline={pipeline} />}
    </div>
  );
}

function RulesBody({ pipeline }: { pipeline: PipelineDocument }) {
  const { data: accounts } = useAccounts();
  const { data: health } = usePipelineHealth();
  const folderName = useFolderNames();
  const { push } = useToast();
  const updateStage = useUpdateStage();
  const deleteStage = useDeleteStage();
  const reorder = useReorderStages();
  const setEnabled = useSetPipelineEnabled();

  const [filter, setFilter] = useState("");
  const [editor, setEditor] = useState<EditorState>(null);
  const [testStageId, setTestStageId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<StageOut | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [describeOpen, setDescribeOpen] = useState(false);
  const [pauseOpen, setPauseOpen] = useState(false);

  // The order shown while a reorder is being saved; the server's takes over
  // once the document refetches.
  const [order, setOrder] = useState<string[]>(() => pipeline.stages.map((s) => s.stage_id));
  useEffect(() => {
    setOrder(pipeline.stages.map((s) => s.stage_id));
  }, [pipeline.stages]);

  const byId = useMemo(() => new Map(pipeline.stages.map((s) => [s.stage_id, s])), [pipeline.stages]);
  const accountById = useMemo(() => new Map((accounts ?? []).map((a) => [a.id, a])), [accounts]);

  const scopeLabel = (stage: StageOut): string => {
    if (stage.accounts === null) return "All accounts";
    if (stage.accounts.length === 0) return "No accounts";
    const names = stage.accounts.map((id) => {
      const a = accountById.get(id);
      return a ? `${a.emoji ? `${a.emoji} ` : ""}${a.name}` : "Unknown account";
    });
    return names.length <= 2 ? names.join(", ") : `${names.length} accounts`;
  };

  const warningsByStage = useMemo(() => {
    const map = new Map<string, RuleWarning[]>();
    for (const w of health ?? pipeline.warnings) {
      if (w.ok) continue;
      const list = map.get(w.stage_id) ?? [];
      list.push({
        account: accountById.get(w.account_id)?.name ?? "An account",
        reference: w.reference,
        detail: w.detail,
      });
      map.set(w.stage_id, list);
    }
    return map;
  }, [health, pipeline.warnings, accountById]);

  const groups = useMemo(() => {
    const out = new Map<RuleGroupId, StageOut[]>();
    for (const id of order) {
      const stage = byId.get(id);
      if (!stage) continue;
      const group = ruleGroup(stage);
      out.set(group, [...(out.get(group) ?? []), stage]);
    }
    return out;
  }, [order, byId]);

  const needle = filter.trim().toLowerCase();
  const matches = (stage: StageOut) =>
    !needle ||
    `${stage.name} ${summaryText(describeRule(stage, folderName))}`.toLowerCase().includes(needle);

  const failedToMove = (err: unknown) => {
    if (err instanceof ApiError && err.status === 409) {
      push("Your rules changed meanwhile — reloaded.", "info");
    } else {
      push(err instanceof ApiError ? err.message : "Could not save the new order", "error");
    }
    setOrder(pipeline.stages.map((s) => s.stage_id));
  };

  const saveOrder = (next: string[]) => {
    if (next === order) return;
    setOrder(next);
    reorder.mutate({ order: next, baseRevision: pipeline.revision }, { onError: failedToMove });
  };

  const sortable = (group: StageOut[]) => group.filter((s) => !isFixedStage(s)).map((s) => s.stage_id);

  const sensors = useSensors(
    useSensor(PointerSensor),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const onDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const stage = byId.get(String(active.id));
    if (!stage) return;
    const members = sortable(groups.get(ruleGroup(stage)) ?? []);
    const from = members.indexOf(String(active.id));
    const to = members.indexOf(String(over.id));
    if (from === -1 || to === -1) return;
    saveOrder(reorderWithinGroup(order, members, arrayMove(members, from, to)));
  };

  const actionsFor = (stage: StageOut, members: string[]): RuleRowActions => {
    const index = members.indexOf(stage.stage_id);
    return {
      onToggle: (enabled) =>
        updateStage.mutate(
          { stageId: stage.stage_id, data: { enabled } },
          { onError: (err) => push(err instanceof ApiError ? err.message : "Could not save", "error") },
        ),
      onEdit: () => setEditor({ stage }),
      onDuplicate: isFixedStage(stage) ? undefined : () => setEditor({ template: stage }),
      onMove: (direction) => saveOrder(moveWithinGroup(order, members, stage.stage_id, direction)),
      canMoveUp: index > 0,
      canMoveDown: index !== -1 && index < members.length - 1,
      onTest: () => setTestStageId(stage.stage_id),
      onDelete: stage.type === "classify" ? undefined : () => setDeleteTarget(stage),
    };
  };

  const visibleGroups = RULE_GROUPS.map((g) => ({
    ...g,
    stages: (groups.get(g.id) ?? []).filter(matches),
    total: (groups.get(g.id) ?? []).length,
  })).filter((g) => g.total > 0);
  const anyShown = visibleGroups.some((g) => g.stages.length > 0);

  return (
    <>
      <div className="flex flex-col gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Rules</h1>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            What happens to your mail as it arrives. Rules run from top to bottom; a rule marked
            stop ends the list for that mail.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={() => setDescribeOpen(true)}>
            <Sparkles className="mr-1 h-3.5 w-3.5" />
            Describe a rule
          </Button>
          <Button variant="outline" onClick={() => setEditor({})}>
            <Plus className="mr-1 h-3.5 w-3.5" />
            Add by hand
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger
              render={<Button variant="ghost" size="icon" aria-label="More rule actions" />}
            >
              <MoreVertical className="h-4 w-4" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="min-w-52">
              <DropdownMenuItem onClick={() => setTestStageId("")}>Test your rules…</DropdownMenuItem>
              <DropdownMenuItem onClick={() => setHistoryOpen(true)}>
                <History className="mr-2 h-3.5 w-3.5" />
                History
              </DropdownMenuItem>
              {pipeline.enabled ? (
                <DropdownMenuItem onClick={() => setPauseOpen(true)}>
                  <PauseCircle className="mr-2 h-3.5 w-3.5" />
                  Pause all rules
                </DropdownMenuItem>
              ) : (
                <DropdownMenuItem
                  onClick={() =>
                    setEnabled.mutate({ enabled: true, baseRevision: pipeline.revision })
                  }
                >
                  Resume rules
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>

      {!pipeline.enabled && (
        <div
          role="status"
          className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm"
        >
          <span>Your rules are paused: nothing acts on your incoming mail.</span>
          <Button
            size="sm"
            variant="outline"
            disabled={setEnabled.isPending}
            onClick={() => setEnabled.mutate({ enabled: true, baseRevision: pipeline.revision })}
          >
            Resume
          </Button>
        </div>
      )}

      <AutomaticFeatures />

      <SectionCard icon={ListFilter} title="Your rules">
        <Input
          aria-label="Filter rules"
          placeholder="Filter by name or what it does"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        {pipeline.stages.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No rules yet. Describe one in a sentence, or add one by hand.
          </p>
        )}
        {pipeline.stages.length > 0 && !anyShown && (
          <p className="text-sm text-muted-foreground">No rule matches “{filter}”.</p>
        )}
        <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
          {visibleGroups
            .filter((g) => g.stages.length > 0)
            .map((g) => {
              const members = sortable(groups.get(g.id) ?? []);
              return (
                <section key={g.id} aria-label={g.label} className="flex flex-col gap-1">
                  <h2 className="px-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                    {g.label} <span className="font-normal">· {g.total}</span>
                  </h2>
                  <div className="overflow-hidden rounded-lg border">
                    <SortableContext
                      items={g.stages.filter((s) => !isFixedStage(s)).map((s) => s.stage_id)}
                      strategy={verticalListSortingStrategy}
                    >
                      {g.stages.map((stage) => {
                        const props = {
                          stage,
                          summary: describeRule(stage, folderName),
                          scope: scopeLabel(stage),
                          warnings: warningsByStage.get(stage.stage_id) ?? [],
                          actions: actionsFor(stage, members),
                        };
                        return isFixedStage(stage) || needle ? (
                          <FixedRuleRow key={stage.stage_id} {...props} />
                        ) : (
                          <SortableRuleRow key={stage.stage_id} {...props} />
                        );
                      })}
                    </SortableContext>
                  </div>
                </section>
              );
            })}
        </DndContext>
      </SectionCard>

      <RuleEditor
        open={editor !== null}
        onOpenChange={(open) => !open && setEditor(null)}
        pipeline={pipeline}
        stage={editor?.stage}
        template={editor?.template}
      />
      <PipelineTestDialog
        open={testStageId !== null}
        onOpenChange={(open) => !open && setTestStageId(null)}
        stageId={testStageId || undefined}
      />
      <DescribeRuleDialog open={describeOpen} onOpenChange={setDescribeOpen} />
      <RevisionsDialog
        open={historyOpen}
        onOpenChange={setHistoryOpen}
        currentRevision={pipeline.revision}
      />
      <ConfirmDialog
        open={pauseOpen}
        onOpenChange={setPauseOpen}
        title="Pause all rules?"
        description="Nothing will act on your incoming mail until you resume. Mail that arrives meanwhile is not processed afterwards."
        confirmLabel="Pause rules"
        confirmVariant="default"
        isConfirming={setEnabled.isPending}
        onConfirm={() =>
          setEnabled.mutate(
            { enabled: false, baseRevision: pipeline.revision },
            { onSuccess: () => setPauseOpen(false) },
          )
        }
      />
      {deleteTarget && (
        <ConfirmDialog
          open
          onOpenChange={(open) => !open && setDeleteTarget(null)}
          title={`Delete “${deleteTarget.name}”?`}
          description="Removes this rule from your list. Mail already processed keeps what it was given; nothing is undone."
          confirmLabel={deleteStage.isPending ? "Deleting…" : "Delete rule"}
          isConfirming={deleteStage.isPending}
          onConfirm={() =>
            deleteStage.mutate(
              { stageId: deleteTarget.stage_id, baseRevision: pipeline.revision },
              {
                onSuccess: () => setDeleteTarget(null),
                onError: (err) => {
                  setDeleteTarget(null);
                  push(err instanceof ApiError ? err.message : "Could not delete the rule", "error");
                },
              },
            )
          }
        />
      )}
    </>
  );
}
