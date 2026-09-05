"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";

import { ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

import { StageConfigForm, seedConfigDefaults } from "@/components/pipeline/stage-config-form";
import { useAccounts } from "@/hooks/use-accounts";
import { useCreateStage, useUpdateStage } from "@/hooks/use-pipeline";
import type { StageOut, StageTypeOut } from "@/types/api";

export function StageFormDialog({
  open,
  onOpenChange,
  stage,
  stageTypes,
  pipelineRevision,
  existingStageIds,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Present when editing; absent when creating a new stage. */
  stage?: StageOut;
  stageTypes: StageTypeOut[];
  pipelineRevision: number;
  existingStageIds: string[];
}) {
  const isEditing = !!stage;
  const { data: accounts } = useAccounts();
  const createStage = useCreateStage();
  const updateStage = useUpdateStage();

  const [stageId, setStageId] = useState(stage?.stage_id ?? "");
  const [type, setType] = useState(stage?.type ?? "");
  const [name, setName] = useState(stage?.name ?? "");
  const [enabled, setEnabled] = useState(stage?.enabled ?? true);
  const [halt, setHalt] = useState(stage?.halt ?? false);
  const [scopeAll, setScopeAll] = useState(stage ? stage.accounts === null : true);
  const [selectedAccounts, setSelectedAccounts] = useState<string[]>(
    stage?.accounts ?? [],
  );
  const [config, setConfig] = useState<Record<string, unknown>>(stage?.config ?? {});
  const [error, setError] = useState<string | null>(null);

  // Reset the form's local state whenever a different stage (or a fresh
  // "create" slot) is opened -- otherwise the previous stage's edits leak
  // into the next dialog instance.
  useEffect(() => {
    if (!open) return;
    setStageId(stage?.stage_id ?? "");
    setType(stage?.type ?? "");
    setName(stage?.name ?? "");
    setEnabled(stage?.enabled ?? true);
    setHalt(stage?.halt ?? false);
    setScopeAll(stage ? stage.accounts === null : true);
    setSelectedAccounts(stage?.accounts ?? []);
    setConfig(stage?.config ?? {});
    setError(null);
  }, [open, stage]);

  const selectedType = stageTypes.find((t) => t.type === type);
  const isPending = createStage.isPending || updateStage.isPending;

  const handleSubmit = () => {
    setError(null);
    const accountsPayload = scopeAll ? null : selectedAccounts;

    if (isEditing) {
      updateStage.mutate(
        {
          stageId: stage.stage_id,
          data: {
            name: name || null,
            config,
            enabled,
            halt,
            accounts: accountsPayload,
            base_revision: pipelineRevision,
          },
        },
        {
          onSuccess: () => onOpenChange(false),
          onError: (err) =>
            setError(err instanceof ApiError ? err.message : String(err)),
        },
      );
    } else {
      if (!stageId.trim()) {
        setError("A stage id is required.");
        return;
      }
      if (existingStageIds.includes(stageId.trim())) {
        setError("A stage with this id already exists.");
        return;
      }
      if (!type) {
        setError("Pick a stage type.");
        return;
      }
      createStage.mutate(
        {
          stage_id: stageId.trim(),
          type,
          name: name || null,
          config,
          enabled,
          halt,
          accounts: accountsPayload,
          base_revision: pipelineRevision,
        },
        {
          onSuccess: () => onOpenChange(false),
          onError: (err) =>
            setError(err instanceof ApiError ? err.message : String(err)),
        },
      );
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg" className="max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isEditing ? `Edit “${stage.name}”` : "Add stage"}</DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          {!isEditing && (
            <>
              <div className="grid gap-1.5">
                <Label className="text-sm">Stage id</Label>
                <Input
                  value={stageId}
                  onChange={(e) => setStageId(e.target.value)}
                  placeholder="archive-newsletters"
                />
                <p className="text-xs text-muted-foreground">
                  Stable and immutable -- this is the trace&apos;s identity.
                </p>
              </div>
              <div className="grid gap-1.5">
                <Label className="text-sm">Type</Label>
                <Select
                  value={type || undefined}
                  onValueChange={(v) => {
                    setType(v ?? "");
                    const t = stageTypes.find((st) => st.type === v);
                    setConfig(t ? seedConfigDefaults(t.schema) : {});
                  }}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Select a stage type…" />
                  </SelectTrigger>
                  <SelectContent>
                    {stageTypes.map((t) => (
                      <SelectItem key={t.type} value={t.type}>
                        {t.type} ({t.runs_on.join(", ")})
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </>
          )}

          {isEditing && (
            <p className="text-xs text-muted-foreground">
              Stage id <span className="font-mono">{stage.stage_id}</span>, type{" "}
              <span className="font-mono">{stage.type}</span> — neither can change once
              created.
            </p>
          )}

          <div className="grid gap-1.5">
            <Label className="text-sm">Name</Label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={type || "Display name"}
            />
          </div>

          <div className="flex items-center gap-6">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                className="h-4 w-4"
                checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
              />
              Enabled
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                className="h-4 w-4"
                checked={halt}
                onChange={(e) => setHalt(e.target.checked)}
              />
              Halt the pipeline after this stage
            </label>
          </div>

          <div className="grid gap-1.5">
            <Label className="text-sm">Accounts</Label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                className="h-4 w-4"
                checked={scopeAll}
                onChange={(e) => setScopeAll(e.target.checked)}
              />
              All accounts
            </label>
            {!scopeAll && (
              <div className="flex flex-col gap-1 rounded-md border p-2">
                {(accounts ?? []).map((a) => (
                  <label key={a.id} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      className="h-4 w-4"
                      checked={selectedAccounts.includes(a.id)}
                      onChange={(e) =>
                        setSelectedAccounts((prev) =>
                          e.target.checked
                            ? [...prev, a.id]
                            : prev.filter((id) => id !== a.id),
                        )
                      }
                    />
                    {a.name}
                  </label>
                ))}
                {(accounts ?? []).length === 0 && (
                  <p className="text-xs text-muted-foreground">No accounts configured.</p>
                )}
              </div>
            )}
          </div>

          <div className="border-t pt-3">
            <Label className="mb-2 block text-sm font-medium">Config</Label>
            {isEditing ? (
              <StageConfigForm
                schema={stageTypes.find((t) => t.type === stage.type)?.schema ?? {}}
                value={config}
                onChange={setConfig}
              />
            ) : selectedType ? (
              <StageConfigForm
                schema={selectedType.schema}
                value={config}
                onChange={setConfig}
              />
            ) : (
              <p className="text-sm text-muted-foreground">Pick a type first.</p>
            )}
          </div>

          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>

        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleSubmit} disabled={isPending}>
            {isPending && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
            {isEditing ? "Save" : "Add stage"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
