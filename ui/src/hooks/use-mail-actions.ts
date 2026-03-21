/**
 * TanStack Query hooks for per-mail actions.
 *
 * Provides individual action mutations with optimistic updates
 * for star toggle, archive, spam, and delete.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { MailActionRequest } from "@/types/api";

/** Shared mutation for a single-mail action. */
function useMailActionMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      mailId,
      accountId,
      action,
    }: {
      mailId: string;
      accountId: string;
      action: MailActionRequest;
    }) => api.mails.action(mailId, accountId, action),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["mails"] });
      qc.invalidateQueries({ queryKey: ["mail"] });
      qc.invalidateQueries({ queryKey: ["folders"] });
    },
  });
}

/** Toggle star (flag/unflag) on a mail. */
export function useStarMail() {
  return useMailActionMutation();
}

/** Archive a mail (move to archive folder). */
export function useArchiveMail() {
  return useMailActionMutation();
}

/** Mark a mail as spam (move to spam folder). */
export function useSpamMail() {
  return useMailActionMutation();
}

/** Delete a mail (soft-delete). */
export function useDeleteMail() {
  return useMailActionMutation();
}
