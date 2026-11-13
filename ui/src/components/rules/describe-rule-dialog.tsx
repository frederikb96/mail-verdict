"use client";

/**
 * The rule assistant's entry from the Rules page: choose the mail the rule
 * is about, then say in a sentence what should happen to mail like it. The
 * proposal that comes back is the same dialog the reading pane opens.
 */

import { useState } from "react";

import { AddRuleDialog } from "@/components/mail/add-rule-dialog";
import { ExampleMailPicker } from "@/components/rules/example-mail-picker";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function DescribeRuleDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [mailId, setMailId] = useState<string | null>(null);

  const close = () => {
    setMailId(null);
    onOpenChange(false);
  };

  if (open && mailId) {
    return <AddRuleDialog mailId={mailId} open onOpenChange={(next) => !next && close()} />;
  }
  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent size="xl" className="max-h-[90dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Describe a rule</DialogTitle>
        </DialogHeader>
        <p className="text-sm text-muted-foreground">
          Pick a mail the rule is about. Next you say in a sentence what should happen to mail
          like it, and the assistant proposes the change for you to accept or decline.
        </p>
        <ExampleMailPicker selectedId={null} onPick={(m) => setMailId(m.id)} />
        <div className="flex justify-end">
          <Button variant="outline" onClick={close}>
            Cancel
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
