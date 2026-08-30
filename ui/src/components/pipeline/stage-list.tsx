"use client";

import { useEffect, useState } from "react";
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  AlertTriangle,
  FlaskConical,
  GripVertical,
  Pencil,
  Plus,
  Trash2,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Switch } from "@/components/ui/switch";

import { PipelineTestDialog } from "@/components/pipeline/pipeline-test-dialog";
import { StageFormDialog } from "@/components/pipeline/stage-form-dialog";
import {
  useDeleteStage,
  usePipelineHealth,
  useReorderStages,
  useUpdateStage,
} from "@/hooks/use-pipeline";
import { useAccounts } from "@/hooks/use-accounts";
import type { PipelineDocument, StageOut, StageTypeOut } from "@/types/api";

function StageRow({
  stage,
  warnings,
  accountNames,
  onEdit,
  onDelete,
  onTest,
}: {
  stage: StageOut;
  warnings: { account_id: string; reference: string; detail: string | null }[];
  accountNames: Map<string, string>;
  onEdit: () => void;
  onDelete: () => void;
  onTest: () => void;
}) {
  const updateStage = useUpdateStage();
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id: stage.stage_id });

  const style = { transform: CSS.Transform.toString(transform), transition };

  const scopeLabel =
    stage.accounts === null
      ? "All accounts"
      : stage.accounts.length === 0
        ? "No accounts"
        : stage.accounts.map((id) => accountNames.get(id) ?? id).join(", ");

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`flex flex-col gap-2 border-b p-3 last:border-b-0 ${isDragging ? "opacity-50" : ""}`}
    >
      <div className="flex items-center gap-2">
        <button className="cursor-grab touch-none" {...attributes} {...listeners}>
          <GripVertical className="h-4 w-4 text-muted-foreground" />
        </button>
        <Badge variant="outline" className="font-mono text-[10px]">
          {stage.type}
        </Badge>
        <span className={`flex-1 truncate text-sm ${!stage.enabled ? "text-muted-foreground line-through" : ""}`}>
          {stage.name}
        </span>
        {stage.halt && (
          <Badge variant="secondary" title="Stops the pipeline when this stage matches">
            halt
          </Badge>
        )}
        <Switch
          checked={stage.enabled}
          onCheckedChange={(checked: boolean) =>
            updateStage.mutate({ stageId: stage.stage_id, data: { enabled: checked } })
          }
        />
        <Button variant="ghost" size="icon-sm" onClick={onTest} title="Dry-run this stage">
          <FlaskConical className="h-3.5 w-3.5" />
        </Button>
        <Button variant="ghost" size="icon-sm" onClick={onEdit}>
          <Pencil className="h-3.5 w-3.5" />
        </Button>
        <Button variant="ghost" size="icon-sm" className="text-destructive" onClick={onDelete}>
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </div>
      <div className="pl-6 text-xs text-muted-foreground">
        {stage.stage_id} &bull; {scopeLabel}
      </div>
      {warnings.length > 0 && (
        <div className="ml-6 flex flex-col gap-1">
          {warnings.map((w, i) => (
            <div
              key={i}
              className="flex items-start gap-1.5 rounded-md border border-destructive/40 bg-destructive/5 px-2 py-1 text-xs text-destructive"
            >
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
              <span>
                {accountNames.get(w.account_id) ?? w.account_id}: &ldquo;{w.reference}
                &rdquo; unresolved{w.detail ? ` — ${w.detail}` : ""}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function StageList({
  pipeline,
  stageTypes,
}: {
  pipeline: PipelineDocument;
  stageTypes: StageTypeOut[];
}) {
  const { data: accounts } = useAccounts();
  const { data: health } = usePipelineHealth();
  const reorderStages = useReorderStages();
  const deleteStage = useDeleteStage();

  const [localStages, setLocalStages] = useState<StageOut[]>(pipeline.stages);
  const [editingStage, setEditingStage] = useState<StageOut | undefined>(undefined);
  const [formOpen, setFormOpen] = useState(false);
  const [testingStageId, setTestingStageId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<StageOut | null>(null);

  useEffect(() => {
    setLocalStages(pipeline.stages);
  }, [pipeline.stages]);

  const accountNames = new Map((accounts ?? []).map((a) => [a.id, a.name]));

  const sensors = useSensors(
    useSensor(PointerSensor),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const oldIndex = localStages.findIndex((s) => s.stage_id === active.id);
    const newIndex = localStages.findIndex((s) => s.stage_id === over.id);
    if (oldIndex === -1 || newIndex === -1) return;

    const updated = [...localStages];
    const [moved] = updated.splice(oldIndex, 1);
    updated.splice(newIndex, 0, moved);
    setLocalStages(updated);
    reorderStages.mutate({
      order: updated.map((s) => s.stage_id),
      baseRevision: pipeline.revision,
    });
  };

  const warningsByStage = new Map<string, typeof pipeline.warnings>();
  for (const w of (health ?? pipeline.warnings)) {
    if (w.ok) continue;
    const list = warningsByStage.get(w.stage_id) ?? [];
    list.push(w);
    warningsByStage.set(w.stage_id, list);
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-3">
        <CardTitle className="text-base">Stages</CardTitle>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => setTestingStageId("")}>
            <FlaskConical className="mr-1 h-3.5 w-3.5" />
            Test pipeline
          </Button>
          <Button
            size="sm"
            onClick={() => {
              setEditingStage(undefined);
              setFormOpen(true);
            }}
          >
            <Plus className="mr-1 h-3.5 w-3.5" />
            Add stage
          </Button>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        {localStages.length === 0 && (
          <p className="p-4 text-sm text-muted-foreground">
            No stages defined -- nothing acts on incoming mail.
          </p>
        )}
        {localStages.length > 0 && (
          <DndContext
            sensors={sensors}
            collisionDetection={closestCenter}
            onDragEnd={handleDragEnd}
          >
            <SortableContext
              items={localStages.map((s) => s.stage_id)}
              strategy={verticalListSortingStrategy}
            >
              {localStages.map((stage) => (
                <StageRow
                  key={stage.stage_id}
                  stage={stage}
                  warnings={warningsByStage.get(stage.stage_id) ?? []}
                  accountNames={accountNames}
                  onEdit={() => {
                    setEditingStage(stage);
                    setFormOpen(true);
                  }}
                  onDelete={() => setDeleteTarget(stage)}
                  onTest={() => setTestingStageId(stage.stage_id)}
                />
              ))}
            </SortableContext>
          </DndContext>
        )}
      </CardContent>

      <StageFormDialog
        open={formOpen}
        onOpenChange={setFormOpen}
        stage={editingStage}
        stageTypes={stageTypes}
        pipelineRevision={pipeline.revision}
        existingStageIds={pipeline.stages.map((s) => s.stage_id)}
      />

      <PipelineTestDialog
        open={testingStageId !== null}
        onOpenChange={(open) => !open && setTestingStageId(null)}
        stageId={testingStageId || undefined}
      />

      {deleteTarget && (
        <ConfirmDialog
          open={!!deleteTarget}
          onOpenChange={(open) => !open && setDeleteTarget(null)}
          title={`Delete “${deleteTarget.name}”?`}
          description="Removes this stage from the pipeline. Mail already processed through it keeps its trace; nothing is undone."
          confirmLabel={deleteStage.isPending ? "Deleting…" : "Delete stage"}
          isConfirming={deleteStage.isPending}
          onConfirm={() =>
            deleteStage.mutate(
              { stageId: deleteTarget.stage_id, baseRevision: pipeline.revision },
              { onSuccess: () => setDeleteTarget(null) },
            )
          }
        />
      )}
    </Card>
  );
}
