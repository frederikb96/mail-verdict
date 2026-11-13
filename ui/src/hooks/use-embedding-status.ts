/** Coverage of the semantic search index, and the manual backfill. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

const key = ["embeddings", "status"] as const;

export function useEmbeddingStatus() {
  return useQuery({
    queryKey: key,
    queryFn: () => api.search.embeddingStatus(),
    staleTime: 30_000,
  });
}

export function useEmbeddingBackfill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.search.embeddingBackfill(),
    onSuccess: (data) => qc.setQueryData(key, data),
  });
}
