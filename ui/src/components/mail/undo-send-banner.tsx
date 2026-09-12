"use client";

import { useEffect, useState } from "react";
import { useAtomValue, useSetAtom } from "jotai";
import { Mail, Undo2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useCancelPendingSend, usePendingSends } from "@/hooks/use-outbox";
import { useAccounts } from "@/hooks/use-accounts";
import { useIdentities } from "@/hooks/use-identities";
import { useToast } from "@/hooks/use-toast";
import { api } from "@/lib/api";
import { composeIntentAtom, selectedAccountIdAtom, isUnifiedViewAtom } from "@/lib/atoms";
import type { Identity, PendingSendResponse } from "@/types/api";

function secondsRemaining(sendAfter: string): number {
  return Math.max(0, Math.ceil((new Date(sendAfter).getTime() - Date.now()) / 1000));
}

/** A cancelled send's staged attachments, fetched back as Files -- pasted
 * images (the ones carrying a content id) apart from the rest, since they
 * go back into the body rather than the attachment list. Any that could
 * not be fetched are named in `missing`. */
async function restoreAttachments(row: PendingSendResponse) {
  const attachments: File[] = [];
  const inlineImages: Array<{ contentId: string; file: File }> = [];
  const missing: string[] = [];
  const results = await Promise.allSettled(
    row.attachments.map(async (att) => {
      const blob = await api.outbox.pendingAttachment(row.id, att.id);
      return new File([blob], att.filename ?? "attachment", {
        type: att.content_type ?? blob.type,
      });
    }),
  );
  results.forEach((result, i) => {
    const att = row.attachments[i];
    if (result.status === "rejected") missing.push(att.filename ?? "attachment");
    else if (att.content_id) inlineImages.push({ contentId: att.content_id, file: result.value });
    else attachments.push(result.value);
  });
  return { attachments, inlineImages, missing };
}

function PendingSendRow({
  row,
  accountName,
  identities,
}: {
  row: PendingSendResponse;
  accountName: string | null;
  identities: Identity[] | undefined;
}) {
  const [remaining, setRemaining] = useState(() => secondsRemaining(row.send_after));
  const cancel = useCancelPendingSend();
  const { push: pushToast } = useToast();
  const setComposeIntent = useSetAtom(composeIntentAtom);

  useEffect(() => {
    const id = setInterval(() => setRemaining(secondsRemaining(row.send_after)), 250);
    return () => clearInterval(id);
  }, [row.send_after]);

  return (
    <div className="flex items-center gap-2 border-b bg-muted px-3 py-1.5 text-sm">
      <Mail className="h-4 w-4 shrink-0 text-muted-foreground" />
      <span>Sending in {remaining}s...</span>
      {accountName && (
        <span className="shrink-0 truncate rounded-full border px-1.5 py-0 text-[10px] text-muted-foreground">
          {accountName}
        </span>
      )}
      <Button
        variant="ghost"
        size="sm"
        className="ml-auto h-6 gap-1 px-2"
        disabled={cancel.isPending}
        onClick={() =>
          cancel.mutate(row.id, {
            onSuccess: async () => {
              pushToast("Send cancelled", "success");
              // Reopens with everything that was staged -- to/cc/bcc,
              // subject, the composed body (quote included, since it was
              // already part of body_html), its attachments and pasted
              // images and, for a reply or a draft-resend, the headers
              // that thread or supersede correctly on a second Send. The
              // row is the one durable copy of all of it once the
              // original composer unmounted.
              const restored = await restoreAttachments(row);
              setComposeIntent({
                attachments: restored.attachments,
                inlineImages: restored.inlineImages,
                unrestoredAttachments: restored.missing,
                accountId: row.account_id,
                identityId: (identities ?? []).find(
                  (i) => i.account_id === row.account_id && i.address === row.from_addr,
                )?.id,
                to: row.to,
                cc: row.cc ?? undefined,
                bcc: row.bcc ?? undefined,
                subject: row.subject ?? undefined,
                bodyHtml: row.body_html ?? undefined,
                inReplyTo: row.in_reply_to ?? undefined,
                references: row.references ?? undefined,
                replacesMessageId: row.replaces_message_id ?? undefined,
              });
            },
            onError: () => pushToast("Too late — the message already sent", "warning"),
          })
        }
      >
        <Undo2 className="h-3.5 w-3.5" />
        Undo
      </Button>
    </div>
  );
}

/** Persistent banner for a send still inside its undo window -- the
 * grace period settings.outbox.undo_send_seconds gives every send before
 * it becomes a real, irreversible outbox row. Not scoped tighter than the
 * current account/unified view, the same choice OutboxDeadBanner makes,
 * so a send from another account doesn't need its own view open to still
 * be cancellable. */
export function UndoSendBanner() {
  const accountId = useAtomValue(selectedAccountIdAtom);
  const isUnified = useAtomValue(isUnifiedViewAtom);
  const { data: pending } = usePendingSends({
    account_id: isUnified || !accountId ? undefined : accountId,
  });
  // A row here can belong to any account whenever this banner isn't
  // scoped to one (unified view, or no account selected yet) -- worth
  // saying only once more than one account exists to tell apart.
  const { data: accounts } = useAccounts();
  const { data: identities } = useIdentities();
  const showAccount = (isUnified || !accountId) && (accounts?.length ?? 0) > 1;

  if (!pending || pending.length === 0) return null;

  return (
    <>
      {pending.map((row) => (
        <PendingSendRow
          key={row.id}
          row={row}
          identities={identities}
          accountName={
            showAccount ? (accounts?.find((a) => a.id === row.account_id)?.name ?? null) : null
          }
        />
      ))}
    </>
  );
}
