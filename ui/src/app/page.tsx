"use client";

import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { BulkPanel } from "@/components/mail/bulk-panel";
import { MailList } from "@/components/mail/mail-list";
import { MobileMailHeader } from "@/components/mail/mobile-mail-header";
import { ReadingPane } from "@/components/mail/reading-pane";
import { ClientOnly } from "@/components/client-only";
import { useSelection } from "@/hooks/use-selection";
import { useIsMobile } from "@/hooks/use-mobile";
import { useMailUrlSync } from "@/hooks/use-mail-url-sync";
import { useRecordUnifiedView } from "@/hooks/use-open-message";
import { useAtomValue, useSetAtom } from "jotai";
import { composeIntentAtom, requestSelectMailAtom, selectedMailIdAtom } from "@/lib/atoms";
import { ArrowLeft, Pencil } from "lucide-react";
import { Button } from "@/components/ui/button";

export default function MailPage() {
  return (
    <ClientOnly>
      <MailView />
    </ClientOnly>
  );
}

/**
 * useMailUrlSync() calls useSearchParams(), which next's static export
 * refuses to prerender without a Suspense boundary -- ClientOnly above is
 * that boundary here, the same reason calendar/page.tsx wraps
 * CalendarPage the same way: nothing renders during the build's
 * prerender pass, so useSearchParams() is never actually called then.
 */
function MailView() {
  useMailUrlSync();
  useRecordUnifiedView();
  const isMobile = useIsMobile();
  const selectedMailId = useAtomValue(selectedMailIdAtom);
  const requestSelectMail = useSetAtom(requestSelectMailAtom);
  const setComposeIntent = useSetAtom(composeIntentAtom);
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
              onClick={() => requestSelectMail(null)}
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
      <div className="relative flex h-full flex-col overflow-hidden">
        <MobileMailHeader />
        <div className="min-h-0 flex-1 overflow-hidden">
          <MailList />
        </div>
        {selectionCount > 0 ? (
          <BulkPanel compact />
        ) : (
          // Compose exists only inside the sidebar sheet otherwise, on a
          // screen with no reading pane to carry a "New message" affordance
          // anywhere else. Hidden rather than overlapping while a
          // selection bar is showing at the same corner of the screen.
          <Button
            size="icon"
            className="absolute bottom-4 right-4 h-12 w-12 rounded-full shadow-lg"
            onClick={() => setComposeIntent({})}
            aria-label="Compose"
            title="Compose"
          >
            <Pencil className="h-5 w-5" />
          </Button>
        )}
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
