"use client";

import { useState } from "react";
import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useOrderCatchUp } from "@/hooks/use-orders";

/** "Look through recent mail…": a number of days, a dry-run preview of how
 * many mails the model would read, then the real sweep -- against the
 * catch-up endpoint's own dry_run flag, so a preview never enqueues
 * anything and Start always sees the count it just asked for. */
export function OrderCatchUpDialog({
  accountId,
  open,
  onOpenChange,
}: {
  accountId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [days, setDays] = useState(90);
  const [preview, setPreview] = useState<number | null>(null);
  const [result, setResult] = useState<number | null>(null);
  const catchUp = useOrderCatchUp();

  const runPreview = () => {
    setResult(null);
    catchUp.mutate(
      { account_id: accountId, days, dry_run: true },
      { onSuccess: (r) => setPreview(r.passed) },
    );
  };

  const runStart = () => {
    catchUp.mutate(
      { account_id: accountId, days, dry_run: false },
      { onSuccess: (r) => setResult(r.queued) },
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next);
        if (!next) {
          setDays(90);
          setPreview(null);
          setResult(null);
        }
      }}
    >
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>Look through recent mail</DialogTitle>
        </DialogHeader>
        <p className="text-sm text-muted-foreground">
          Reads mail already in this account's mailbox for orders and tickets it missed --
          nothing is classified twice, whatever you choose here.
        </p>
        <div className="grid gap-1.5">
          <Label htmlFor="catch-up-days">Days</Label>
          <Input
            id="catch-up-days"
            type="number"
            min={1}
            max={365}
            value={days}
            onChange={(e) => {
              setDays(Number(e.target.value));
              setPreview(null);
              setResult(null);
            }}
          />
        </div>
        {preview !== null && result === null && (
          <p className="text-sm">
            {preview} mail{preview === 1 ? "" : "s"} would be read by the model.
          </p>
        )}
        {result !== null && (
          <p className="text-sm">
            {result} mail{result === 1 ? "" : "s"} queued.
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
          {result === null && (
            <Button variant="outline" disabled={catchUp.isPending} onClick={runPreview}>
              {catchUp.isPending && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
              Preview
            </Button>
          )}
          {preview !== null && result === null && (
            <Button disabled={catchUp.isPending} onClick={runStart}>
              {catchUp.isPending && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
              Start
            </Button>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
