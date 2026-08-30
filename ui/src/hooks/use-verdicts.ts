/** TanStack Query hooks for verdict feedback. */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function useVerdictFeedback() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      mailId,
      accountId,
      isSpam,
    }: {
      mailId: string;
      accountId: string;
      isSpam: boolean;
    }) => api.verdicts.feedback(mailId, accountId, isSpam),
    onSuccess: (_data, { mailId }) => {
      qc.invalidateQueries({ queryKey: ["mail", mailId] });
      qc.invalidateQueries({ queryKey: ["thread", mailId] });
      qc.invalidateQueries({ queryKey: ["mails"] });
    },
  });
}
