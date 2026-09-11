/** TanStack Query hooks for verdict feedback. */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { refreshMailViews } from "@/hooks/use-mails";
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
    // A ruling moves the message as well as recording it -- the folder
    // counts need the same refresh a mail action's own move gets.
    onSuccess: (_data, { mailId }) => {
      qc.invalidateQueries({ queryKey: ["mail", mailId] });
      qc.invalidateQueries({ queryKey: ["thread", mailId] });
      refreshMailViews(qc);
    },
  });
}
