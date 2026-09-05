"use client";

import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { BulkPanel } from "@/components/mail/bulk-panel";
import { MailList } from "@/components/mail/mail-list";
import { ReadingPane } from "@/components/mail/reading-pane";
import { useSelection } from "@/hooks/use-selection";
import { useIsMobile } from "@/hooks/use-mobile";
import { useAtom } from "jotai";
import { selectedMailIdAtom } from "@/lib/atoms";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";

export default function MailPage() {
  const isMobile = useIsMobile();
  const [selectedMailId, setSelectedMailId] = useAtom(selectedMailIdAtom);
  const { count: selectionCount } = useSelection();

  // Mobile: show either mail list or reading pane (not both)
  if (isMobile) {
    if (selectedMailId) {
      return (
        <div className="flex h-full flex-col overflow-hidden">
          <div className="flex items-center border-b px-2 py-1">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setSelectedMailId(null)}
              className="gap-1"
            >
              <ArrowLeft className="h-4 w-4" />
              Back
            </Button>
          </div>
          <div className="min-h-0 flex-1 overflow-hidden">
            <ReadingPane />
          </div>
        </div>
      );
    }
    // A phone has no reading pane to put the bulk panel in, and no hover
    // controls on a row either -- without this a selection made by long
    // press is a dead end.
    return (
      <div className="flex h-full flex-col overflow-hidden">
        <div className="min-h-0 flex-1 overflow-hidden">
          <MailList />
        </div>
        {selectionCount > 0 && <BulkPanel compact />}
      </div>
    );
  }

  // Desktop: two-pane layout with fixed mail list width
  return (
    <div className="flex h-full">
      <div className="flex h-full w-[400px] min-w-[300px] max-w-[600px] flex-shrink-0 flex-col overflow-hidden border-r">
        <MailList />
      </div>
      <div className="flex h-full min-w-0 flex-1 flex-col overflow-hidden">
        <ReadingPane />
      </div>
    </div>
  );
}
