/** TanStack Query hooks for the pipeline definition. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
  PipelineTestRequest,
  StageCreateRequest,
  StageUpdateRequest,
} from "@/types/api";

export const pipelineKeys = {
  document: ["pipeline"] as const,
  stageTypes: ["pipeline", "stage-types"] as const,
  revisions: ["pipeline", "revisions"] as const,
  health: ["pipeline", "health"] as const,
};

export function usePipeline() {
  return useQuery({
    queryKey: pipelineKeys.document,
    queryFn: () => api.pipeline.get(),
    staleTime: 10_000,
  });
}

export function useStageTypes() {
  return useQuery({
    queryKey: pipelineKeys.stageTypes,
    queryFn: () => api.pipeline.stageTypes(),
    staleTime: 5 * 60_000,
  });
}

export function usePipelineRevisions() {
  return useQuery({
    queryKey: pipelineKeys.revisions,
    queryFn: () => api.pipeline.revisions(),
    staleTime: 30_000,
  });
}

/** Folder-resolution state per stage per account -- unresolved references
 * are accepted at write time (folders arrive asynchronously), so this is
 * how they become visible after the fact. */
export function usePipelineHealth() {
  return useQuery({
    queryKey: pipelineKeys.health,
    queryFn: () => api.pipeline.health(),
    staleTime: 15_000,
    refetchInterval: 30_000,
  });
}

export function useSetPipelineEnabled() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      enabled,
      baseRevision,
    }: {
      enabled: boolean;
      baseRevision?: number;
    }) => api.pipeline.replace({ enabled, base_revision: baseRevision }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: pipelineKeys.document });
      qc.invalidateQueries({ queryKey: pipelineKeys.health });
    },
  });
}

export function useCreateStage() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: StageCreateRequest) => api.pipeline.createStage(data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: pipelineKeys.document });
      qc.invalidateQueries({ queryKey: pipelineKeys.health });
    },
  });
}

export function useUpdateStage() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      stageId,
      data,
    }: {
      stageId: string;
      data: StageUpdateRequest;
    }) => api.pipeline.updateStage(stageId, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: pipelineKeys.document });
      qc.invalidateQueries({ queryKey: pipelineKeys.health });
    },
  });
}

export function useDeleteStage() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      stageId,
      baseRevision,
    }: {
      stageId: string;
      baseRevision?: number;
    }) => api.pipeline.deleteStage(stageId, baseRevision),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: pipelineKeys.document });
      qc.invalidateQueries({ queryKey: pipelineKeys.health });
    },
  });
}

export function useReorderStages() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      order,
      baseRevision,
    }: {
      order: string[];
      baseRevision?: number;
    }) => api.pipeline.reorderStages(order, baseRevision),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: pipelineKeys.document });
    },
  });
}

export function useRestorePipelineRevision() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (revision: number) => api.pipeline.restoreRevision(revision),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: pipelineKeys.document });
      qc.invalidateQueries({ queryKey: pipelineKeys.revisions });
      qc.invalidateQueries({ queryKey: pipelineKeys.health });
    },
  });
}

/** Dry-run the whole pipeline, or one stage, against an existing message. */
export function useTestPipeline() {
  return useMutation({
    mutationFn: (data: PipelineTestRequest) => api.pipeline.test(data),
  });
}

export function useTestStage() {
  return useMutation({
    mutationFn: ({
      stageId,
      data,
    }: {
      stageId: string;
      data: PipelineTestRequest;
    }) => api.pipeline.testStage(stageId, data),
  });
}
