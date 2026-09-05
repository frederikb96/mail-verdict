"use client";

import { useEffect, useState } from "react";
import { useAtomValue } from "jotai";
import { Mail, Undo2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useCancelPendingSend, usePendingSends } from "@/hooks/use-outbox";
import { useToast } from "@/hooks/use-toast";
import { selectedAccountIdAtom, isUnifiedViewAtom } from "@/lib/atoms";
import type { PendingSendResponse } from "@/types/api";

function secondsRemaining(sendAfter: string): number {
  return Math.max(0, Math.ceil((new Date(sendAfter).getTime() - Date.now()) / 1000));
}

function PendingSendRow({ row }: { row: PendingSendResponse }) {
  const [remaining, setRemaining] = useState(() => secondsRemaining(row.send_after));
  const cancel = useCancelPendingSend();
  const { push: pushToast } = useToast();

  useEffect(() => {
    const id = setInterval(() => setRemaining(secondsRemaining(row.send_after)), 250);
    return () => clearInterval(id);
  }, [row.send_after]);

  return (
    <div className="flex items-center gap-2 border-b bg-muted px-3 py-1.5 text-sm">
      <Mail className="h-4 w-4 shrink-0 text-muted-foreground" />
      <span>Sending in {remaining}s...</span>
      <Button
        variant="ghost"
        size="sm"
        className="ml-auto h-6 gap-1 px-2"
        disabled={cancel.isPending}
        onClick={() =>
          cancel.mutate(row.id, {
            onSuccess: () => pushToast("Send cancelled", "success"),
            onError: () => pushToast("Too late -- the message already sent", "warning"),
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

  if (!pending || pending.length === 0) return null;

  return (
    <>
      {pending.map((row) => (
        <PendingSendRow key={row.id} row={row} />
      ))}
    </>
  );
}
