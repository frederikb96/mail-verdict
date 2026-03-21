/**
 * Hooks for account emoji management.
 *
 * Shared between web and React Native.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

/** Mutation to set or clear an account's emoji. */
export function useUpdateAccountEmoji() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      accountId,
      emoji,
    }: {
      accountId: string;
      emoji: string | null;
    }) => api.unified.setEmoji(accountId, emoji),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["accounts"] });
      queryClient.invalidateQueries({ queryKey: ["unified"] });
    },
  });
}
