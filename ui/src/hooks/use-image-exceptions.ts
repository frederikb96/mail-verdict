/** TanStack Query hooks for image exception operations. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ImageExceptionCreate } from "@/types/api";

export const imageExceptionKeys = {
  list: (accountId: string) => ["image-exceptions", accountId] as const,
  check: (accountId: string, sender: string) =>
    ["image-exceptions", "check", accountId, sender] as const,
};

export function useImageExceptions(accountId: string | null) {
  return useQuery({
    queryKey: imageExceptionKeys.list(accountId!),
    queryFn: () => api.imageExceptions.list(accountId!),
    enabled: !!accountId,
    staleTime: 60_000,
  });
}

export function useCheckImageException(
  accountId: string | null,
  sender: string | null,
) {
  return useQuery({
    queryKey: imageExceptionKeys.check(accountId!, sender!),
    queryFn: () => api.imageExceptions.check(accountId!, sender!),
    enabled: !!accountId && !!sender,
    staleTime: 60_000,
  });
}

export function useCreateImageException() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      accountId,
      data,
    }: {
      accountId: string;
      data: ImageExceptionCreate;
    }) => api.imageExceptions.create(accountId, data),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: imageExceptionKeys.list(variables.accountId),
      });
      // Invalidate mail and thread queries to refresh image blocking status
      // -- the reading pane reads from the thread cache, not the mail one.
      queryClient.invalidateQueries({ queryKey: ["mail"] });
      queryClient.invalidateQueries({ queryKey: ["thread"] });
    },
  });
}

export function useDeleteImageException() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      accountId,
      exceptionId,
    }: {
      accountId: string;
      exceptionId: string;
    }) => api.imageExceptions.delete(accountId, exceptionId),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: imageExceptionKeys.list(variables.accountId),
      });
      queryClient.invalidateQueries({ queryKey: ["mail"] });
      queryClient.invalidateQueries({ queryKey: ["thread"] });
    },
  });
}
