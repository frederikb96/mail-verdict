/** TanStack Query hooks for settings operations. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const settingsKeys = {
  all: ["settings"] as const,
  category: (cat: string) => ["settings", cat] as const,
};

export function useAllSettings() {
  return useQuery({
    queryKey: settingsKeys.all,
    queryFn: () => api.settings.getAll(),
    staleTime: 5 * 60_000,
  });
}

export function useSettings(category: string) {
  return useQuery({
    queryKey: settingsKeys.category(category),
    queryFn: () => api.settings.get(category),
    staleTime: 5 * 60_000,
  });
}

export function useUpdateSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      category,
      data,
    }: {
      category: string;
      data: Record<string, unknown>;
    }) => api.settings.update(category, data),
    onSuccess: (_data, { category }) => {
      qc.invalidateQueries({ queryKey: settingsKeys.all });
      qc.invalidateQueries({ queryKey: settingsKeys.category(category) });
    },
  });
}
