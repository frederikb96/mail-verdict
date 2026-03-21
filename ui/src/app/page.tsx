"use client";

import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { MailList } from "@/components/mail/mail-list";
import { ReadingPane } from "@/components/mail/reading-pane";
import { useIsMobile } from "@/hooks/use-mobile";
import { useAtom } from "jotai";
import { selectedMailIdAtom } from "@/lib/atoms";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";

export default function MailPage() {
  const isMobile = useIsMobile();
  const [selectedMailId, setSelectedMailId] = useAtom(selectedMailIdAtom);

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
    return (
      <div className="flex h-full flex-col overflow-hidden">
        <MailList />
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
