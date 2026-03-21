"use client";

import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { MailList } from "@/components/mail/mail-list";
import { ReadingPane } from "@/components/mail/reading-pane";

export default function MailPage() {
  return (
    <ResizablePanelGroup orientation="horizontal" className="h-full">
      <ResizablePanel defaultSize={40} minSize={25} maxSize={60}>
        <div className="flex h-full flex-col overflow-hidden border-r">
          <MailList />
        </div>
      </ResizablePanel>
      <ResizableHandle withHandle />
      <ResizablePanel defaultSize={60} minSize={30}>
        <div className="flex h-full flex-col overflow-hidden">
          <ReadingPane />
        </div>
      </ResizablePanel>
    </ResizablePanelGroup>
  );
}
