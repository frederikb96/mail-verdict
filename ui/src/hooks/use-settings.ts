/** TanStack Query hooks for settings operations. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useToast } from "@/hooks/use-toast";

export const settingsKeys = {
  all: ["settings"] as const,
  category: (cat: string) => ["settings", cat] as const,
  providers: ["settings", "providers"] as const,
};

export function useAllSettings() {
  return useQuery({
    queryKey: settingsKeys.all,
    queryFn: () => api.settings.getAll(),
    staleTime: 5 * 60_000,
  });
}

/** Provider keys and the health of every AI task's breaker. Under the
 * ["settings"] prefix, so a settings write or settings.changed refreshes it. */
export function useProvidersStatus() {
  return useQuery({
    queryKey: settingsKeys.providers,
    queryFn: () => api.settings.providers(),
    staleTime: 30_000,
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
  const { push: pushToast } = useToast();
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
    // A rejected PUT (bad type, failed validation) otherwise leaves the
    // form showing the edited value with no sign the save never landed --
    // the caller's own onSuccess is what clears its "unsaved" state, and a
    // rejected mutation never calls it, but nothing said why.
    onError: (err, { category }) => {
      pushToast(`Could not save ${category} settings: ${err.message}`, "error", 0);
    },
  });
}
