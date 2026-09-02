/** TanStack Query hooks for the invitation card rendered inside a mail message. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ImportInvitationRequest } from "@/types/api";

export const invitationKeys = {
  detail: (messageId: string) => ["invitation", messageId] as const,
};

export function useInvitation(messageId: string | null) {
  return useQuery({
    queryKey: invitationKeys.detail(messageId ?? ""),
    queryFn: () => api.invitations.get(messageId!),
    enabled: !!messageId,
    staleTime: 30_000,
  });
}

export function useImportInvitation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      messageId,
      data,
    }: {
      messageId: string;
      data: ImportInvitationRequest;
    }) => api.invitations.import(messageId, data),
    onSuccess: (_result, { messageId }) => {
      qc.invalidateQueries({ queryKey: invitationKeys.detail(messageId) });
      qc.invalidateQueries({ queryKey: ["calendar-events"] });
    },
  });
}
