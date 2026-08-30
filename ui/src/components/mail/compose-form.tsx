"use client";

import { useRef, useState } from "react";
import { Loader2, Paperclip, Send, Save, X, ChevronDown } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { useCreateOutbox } from "@/hooks/use-outbox";
import { useToast } from "@/hooks/use-toast";
import { parseAddressList } from "@/lib/format";
import type { OutboxCreateRequest } from "@/types/api";

interface ComposeFormProps {
  accountId: string;
  defaultTo?: string[];
  defaultCc?: string[];
  defaultSubject?: string;
  defaultBody?: string;
  inReplyTo?: string;
  references?: string[];
  /** Inline reply box styling instead of a standalone dialog form. */
  compact?: boolean;
  onDone: () => void;
}

/** Shared body for the new-mail dialog and the inline reply box. */
export function ComposeForm({
  accountId,
  defaultTo = [],
  defaultCc = [],
  defaultSubject = "",
  defaultBody = "",
  inReplyTo,
  references,
  compact = false,
  onDone,
}: ComposeFormProps) {
  const [to, setTo] = useState(defaultTo.join(", "));
  const [cc, setCc] = useState(defaultCc.join(", "));
  const [bcc, setBcc] = useState("");
  const [showCcBcc, setShowCcBcc] = useState(defaultCc.length > 0);
  const [subject, setSubject] = useState(defaultSubject);
  const [body, setBody] = useState(defaultBody);
  const [attachments, setAttachments] = useState<File[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const createOutbox = useCreateOutbox();
  const { push: pushToast } = useToast();

  const buildRequest = (kind: "send" | "draft"): OutboxCreateRequest => ({
    account_id: accountId,
    kind,
    to: parseAddressList(to),
    cc: parseAddressList(cc),
    bcc: parseAddressList(bcc),
    subject,
    body_text: body,
    in_reply_to: inReplyTo,
    references,
  });

  const submit = (kind: "send" | "draft") => {
    const data = buildRequest(kind);
    if (kind === "send" && data.to.length === 0) {
      pushToast("Add at least one recipient", "warning");
      return;
    }
    createOutbox.mutate(
      { data, attachments },
      {
        onSuccess: () => {
          pushToast(
            kind === "send" ? "Message queued for sending" : "Draft saved",
            "success",
          );
          onDone();
        },
        onError: (err) => {
          pushToast(`Failed to queue message: ${err.message}`, "error", 0);
        },
      },
    );
  };

  return (
    <div className="flex flex-col gap-2">
      {!compact && (
        <Input
          value={to}
          onChange={(e) => setTo(e.target.value)}
          placeholder="To"
        />
      )}
      {compact && (
        <div className="grid grid-cols-[auto_1fr] items-center gap-2">
          <span className="text-xs text-muted-foreground">To</span>
          <Input
            value={to}
            onChange={(e) => setTo(e.target.value)}
            placeholder="Recipients"
            className="h-8"
          />
        </div>
      )}

      {!showCcBcc && (
        <button
          type="button"
          className="flex w-fit items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          onClick={() => setShowCcBcc(true)}
        >
          Cc/Bcc
          <ChevronDown className="h-3 w-3" />
        </button>
      )}
      {showCcBcc && (
        <>
          <Input
            value={cc}
            onChange={(e) => setCc(e.target.value)}
            placeholder="Cc"
            className={compact ? "h-8" : undefined}
          />
          <Input
            value={bcc}
            onChange={(e) => setBcc(e.target.value)}
            placeholder="Bcc"
            className={compact ? "h-8" : undefined}
          />
        </>
      )}

      {!compact && (
        <Input
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          placeholder="Subject"
        />
      )}

      <Textarea
        value={body}
        onChange={(e) => setBody(e.target.value)}
        placeholder="Write your message..."
        rows={compact ? 5 : 10}
        autoFocus={compact}
      />

      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {attachments.map((file, i) => (
            <Badge key={`${file.name}-${i}`} variant="outline" className="gap-1">
              <Paperclip className="h-3 w-3" />
              {file.name}
              <button
                type="button"
                onClick={() =>
                  setAttachments((prev) => prev.filter((_, idx) => idx !== i))
                }
              >
                <X className="h-3 w-3" />
              </button>
            </Badge>
          ))}
        </div>
      )}

      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1">
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={(e) => {
              const files = Array.from(e.target.files ?? []);
              setAttachments((prev) => [...prev, ...files]);
              e.target.value = "";
            }}
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={() => fileInputRef.current?.click()}
            title="Attach files"
          >
            <Paperclip className="h-4 w-4" />
          </Button>
        </div>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={createOutbox.isPending}
            onClick={() => submit("draft")}
          >
            <Save className="mr-1 h-3.5 w-3.5" />
            Save draft
          </Button>
          <Button
            type="button"
            size="sm"
            disabled={createOutbox.isPending}
            onClick={() => submit("send")}
          >
            {createOutbox.isPending ? (
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
            ) : (
              <Send className="mr-1 h-3.5 w-3.5" />
            )}
            Send
          </Button>
        </div>
      </div>
    </div>
  );
}
