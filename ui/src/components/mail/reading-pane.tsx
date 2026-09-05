"use client";

import { useEffect, useRef, useState } from "react";
import { useAtomValue, useSetAtom } from "jotai";
import {
  Mail,
  ShieldAlert,
  Trash2,
  Star,
  Archive,
  Ban,
  ThumbsUp,
  ThumbsDown,
  MailOpen,
  Mail as MailIcon,
  FileDown,
  Search,
  ChevronUp,
  ChevronDown,
  X,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { ReplyBox } from "@/components/mail/reply-box";
import { DraftEditor } from "@/components/mail/draft-editor";
import { ThreadMessage } from "@/components/mail/thread-message";
import { BulkPanel } from "@/components/mail/bulk-panel";
import { api } from "@/lib/api";
import { useMailAction, useThread } from "@/hooks/use-mails";
import { useVerdictFeedback } from "@/hooks/use-verdicts";
import { useAccount } from "@/hooks/use-accounts";
import { useFolders } from "@/hooks/use-folders";
import { useSelection } from "@/hooks/use-selection";
import { isEditableElement } from "@/lib/utils";
import { selectedMailIdAtom } from "@/lib/atoms";

export function ReadingPane() {
  const mailId = useAtomValue(selectedMailIdAtom);
  const setSelectedMailId = useSetAtom(selectedMailIdAtom);
  const { count: selectionCount } = useSelection();
  const { data: thread, isLoading } = useThread(mailId);
  const mailAction = useMailAction();
  const verdictFeedback = useVerdictFeedback();
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [imageOverrides, setImageOverrides] = useState<Set<string>>(new Set());
  const [confirmExpunge, setConfirmExpunge] = useState(false);
  const autoReadRef = useRef<string | null>(null);

  const messages = thread?.messages ?? [];
  const primary =
    messages.find((m) => m.id === mailId) ?? messages[messages.length - 1] ?? null;
  const account = useAccount(primary?.account_id ?? null);
  const { data: folders } = useFolders(primary?.account_id ?? null);
  const isDraft = primary?.is_draft ?? false;
  // Permanent delete is only offered inside Trash -- everywhere else "delete"
  // means the reversible move-to-trash button already above it.
  const isInTrash =
    folders?.find((f) => f.id === primary?.folder_id)?.special_use === "trash";
  // Junk offers "remove from junk" instead of "move to junk" -- classifying
  // an already-junked message as spam again is a no-op the API doesn't need.
  const isInJunk =
    folders?.find((f) => f.id === primary?.folder_id)?.special_use === "junk";

  // Reset expansion state per opened mail: last message expanded, plus
  // whichever message the user actually clicked in the list.
  useEffect(() => {
    if (messages.length === 0) return;
    const next = new Set<string>();
    const last = messages[messages.length - 1];
    next.add(last.id);
    if (mailId) next.add(mailId);
    setExpandedIds(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mailId, messages.length > 0 ? messages[0].id : null]);

  // Auto mark-as-read the specific message the user opened -- skipped for a
  // draft, which is about to be edited or replaced rather than read.
  useEffect(() => {
    if (primary && !isDraft && !primary.is_seen && primary.id !== autoReadRef.current) {
      autoReadRef.current = primary.id;
      mailAction.mutate({
        mailId: primary.id,
        accountId: primary.account_id,
        action: { action: "mark_read" },
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [primary?.id, primary?.is_seen, isDraft]);

  // Finds text inside the specific message this pane has open -- the
  // shadow root EmailRenderer draws it in is invisible to the browser's
  // own ctrl+F, which is the entire reason this exists. Scoped to primary
  // rather than every message in the thread, matching the singular
  // framing of the feature; findQuery/activeMatchIndex only ever reach the
  // one ThreadMessage instance whose mail.id === primary.id, below.
  const [findOpen, setFindOpen] = useState(false);
  const [findQuery, setFindQuery] = useState("");
  const [activeMatchIndex, setActiveMatchIndex] = useState(0);
  const [matchCount, setMatchCount] = useState(0);
  const findInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setFindOpen(false);
    setFindQuery("");
    setActiveMatchIndex(0);
    setMatchCount(0);
  }, [mailId]);

  useEffect(() => {
    if (findOpen) findInputRef.current?.focus();
  }, [findOpen]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
        if (!primary || isEditableElement(e.target)) return;
        e.preventDefault();
        setFindOpen(true);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [primary]);

  const stepMatch = (direction: 1 | -1) => {
    setActiveMatchIndex((prev) => {
      if (matchCount === 0) return 0;
      return (prev + direction + matchCount) % matchCount;
    });
  };

  const toggle = (id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // More than one message selected replaces the reading pane with the
  // bulk panel entirely; a single-or-no selection leaves it exactly as
  // below, whatever message (if any) happens to be open.
  if (selectionCount > 1) {
    return <BulkPanel />;
  }

  if (!mailId) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
        <Mail className="h-16 w-16 opacity-30" />
        <p className="text-sm">Select a message to read</p>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex flex-col gap-4 p-6">
        <Skeleton className="h-8 w-3/4" />
        <Skeleton className="h-4 w-1/2" />
        <Skeleton className="h-4 w-1/3" />
        <Separator />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (!primary) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
        <ShieldAlert className="h-12 w-12 opacity-50" />
        <p className="text-sm">Message not found</p>
      </div>
    );
  }

  if (isDraft) {
    return <DraftEditor mail={primary} onDone={() => setSelectedMailId(null)} />;
  }

  return (
    <div className="flex h-full flex-col">
      {/* Subject and the consolidated action row for the opened message. */}
      <div className="flex items-start justify-between gap-4 border-b p-4">
        <h2 className="text-lg font-semibold leading-tight">
          {primary.subject ?? "(no subject)"}
        </h2>
        <div role="toolbar" aria-label="Message actions" className="flex shrink-0 items-center gap-1">
          {messages.length > 1 && (
            <Badge variant="secondary" className="mr-1">
              {messages.length} messages
            </Badge>
          )}
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={() => setFindOpen(true)}
            title="Find in message"
            aria-label="Find in message"
          >
            <Search className="h-4 w-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={() =>
              mailAction.mutate({
                mailId: primary.id,
                accountId: primary.account_id,
                action: { action: primary.is_flagged ? "unflag" : "flag" },
              })
            }
            title={primary.is_flagged ? "Unstar" : "Star"}
            aria-label={primary.is_flagged ? "Unstar" : "Star"}
          >
            <Star
              className={
                primary.is_flagged ? "h-4 w-4 fill-yellow-400 text-yellow-400" : "h-4 w-4"
              }
            />
          </Button>
          <a
            href={api.mails.rawUrl(primary.id)}
            download={`${primary.subject ?? "message"}.eml`}
            className="flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
            title="Download as .eml"
            aria-label="Download as .eml"
          >
            <FileDown className="h-4 w-4" />
          </a>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={() =>
              mailAction.mutate({
                mailId: primary.id,
                accountId: primary.account_id,
                action: { action: primary.is_seen ? "mark_unread" : "mark_read" },
              })
            }
            title={primary.is_seen ? "Mark as unread" : "Mark as read"}
            aria-label={primary.is_seen ? "Mark as unread" : "Mark as read"}
          >
            {primary.is_seen ? <MailIcon className="h-4 w-4" /> : <MailOpen className="h-4 w-4" />}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={() =>
              mailAction.mutate({
                mailId: primary.id,
                accountId: primary.account_id,
                action: { action: "archive" },
              })
            }
            title="Archive"
            aria-label="Archive"
          >
            <Archive className="h-4 w-4" />
          </Button>
          {/* The verdict thumb corrects the model's classification; the
              Junk control below it moves the message on the server. Two
              different actions, kept visually apart in the row. */}
          {primary.verdict?.is_spam && (
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              onClick={() =>
                verdictFeedback.mutate({
                  mailId: primary.id, accountId: primary.account_id, isSpam: false,
                })
              }
              title="Mark verdict as not spam"
              aria-label="Mark verdict as not spam"
            >
              <ThumbsUp className="h-4 w-4" />
            </Button>
          )}
          {primary.verdict && !primary.verdict.is_spam && (
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              onClick={() =>
                verdictFeedback.mutate({
                  mailId: primary.id, accountId: primary.account_id, isSpam: true,
                })
              }
              title="Mark verdict as spam"
              aria-label="Mark verdict as spam"
            >
              <ThumbsDown className="h-4 w-4" />
            </Button>
          )}
          {isInJunk ? (
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              onClick={() =>
                mailAction.mutate({
                  mailId: primary.id,
                  accountId: primary.account_id,
                  action: { action: "not_spam" },
                })
              }
              title="Remove from Junk"
              aria-label="Remove from Junk"
            >
              <ThumbsUp className="h-4 w-4" />
            </Button>
          ) : (
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              onClick={() =>
                mailAction.mutate({
                  mailId: primary.id,
                  accountId: primary.account_id,
                  action: { action: "spam" },
                })
              }
              title="Move to Junk"
              aria-label="Move to Junk"
            >
              <Ban className="h-4 w-4" />
            </Button>
          )}
          {isInTrash ? (
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 text-destructive"
              onClick={() => setConfirmExpunge(true)}
              title="Delete forever"
              aria-label="Delete forever"
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          ) : (
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              onClick={() =>
                mailAction.mutate({
                  mailId: primary.id,
                  accountId: primary.account_id,
                  action: { action: "trash" },
                })
              }
              title="Move to trash"
              aria-label="Move to trash"
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={confirmExpunge}
        onOpenChange={setConfirmExpunge}
        title="Delete this message forever?"
        description="This removes it from the mail server. It cannot be undone."
        isConfirming={mailAction.isPending}
        onConfirm={() =>
          mailAction.mutate(
            {
              mailId: primary.id,
              accountId: primary.account_id,
              action: { action: "expunge" },
            },
            { onSuccess: () => setConfirmExpunge(false) },
          )
        }
      />

      <div className="relative min-h-0 flex-1">
        {findOpen && (
          <div className="absolute inset-x-0 top-0 z-10 flex items-center gap-2 border-b bg-background p-2 shadow-sm">
            <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
            <Input
              ref={findInputRef}
              value={findQuery}
              onChange={(e) => {
                setFindQuery(e.target.value);
                setActiveMatchIndex(0);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  stepMatch(e.shiftKey ? -1 : 1);
                } else if (e.key === "Escape") {
                  setFindOpen(false);
                }
              }}
              placeholder="Find in message"
              aria-label="Find in message"
              className="h-7 flex-1"
            />
            <span className="shrink-0 text-xs text-muted-foreground">
              {matchCount > 0
                ? `${activeMatchIndex + 1} of ${matchCount}`
                : findQuery
                  ? "No matches"
                  : ""}
            </span>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              disabled={matchCount === 0}
              onClick={() => stepMatch(-1)}
              title="Previous match"
              aria-label="Previous match"
            >
              <ChevronUp className="h-4 w-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              disabled={matchCount === 0}
              onClick={() => stepMatch(1)}
              title="Next match"
              aria-label="Next match"
            >
              <ChevronDown className="h-4 w-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              onClick={() => setFindOpen(false)}
              title="Close find"
              aria-label="Close find"
            >
              <X className="h-4 w-4" />
            </Button>
          </div>
        )}
        <div className="h-full overflow-auto">
          {messages.map((m) => (
            <ThreadMessage
              key={m.id}
              mail={m}
              expanded={expandedIds.has(m.id)}
              onToggle={() => toggle(m.id)}
              imagesAllowedOverride={imageOverrides.has(m.id)}
              onLoadImages={() =>
                setImageOverrides((prev) => new Set(prev).add(m.id))
              }
              searchQuery={findOpen && m.id === primary.id ? findQuery : undefined}
              activeMatchIndex={findOpen && m.id === primary.id ? activeMatchIndex : undefined}
              onMatchCountChange={m.id === primary.id ? setMatchCount : undefined}
            />
          ))}
        </div>
      </div>

      {messages.length > 0 && (
        <ReplyBox
          source={messages[messages.length - 1]}
          ownEmail={account.data?.imap_user ?? ""}
        />
      )}
    </div>
  );
}
