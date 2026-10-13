"use client";

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { api, ApiError } from "@/lib/api";
import { pipelineKeys } from "@/hooks/use-pipeline";
import { useToast } from "@/hooks/use-toast";
import type {
  DiffRow,
  RuleAssistantChange,
  RuleAssistantResponse,
  RuleAssistantRuleChange,
} from "@/types/api";

const MAX_PROMPT_CHARS = 1000;

type View =
  | { kind: "prompt" }
  | { kind: "running" }
  | { kind: "result"; response: RuleAssistantResponse; change: RuleAssistantChange }
  | { kind: "empty"; message: string }
  | { kind: "error"; message: string };

interface AddRuleDialogProps {
  /** The open mail the sentence is about. */
  mailId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

function describeError(err: unknown): string {
  if (err instanceof DOMException && err.name === "TimeoutError") {
    return "The assistant took too long. Try again.";
  }
  if (err instanceof ApiError) return err.message;
  return err instanceof Error ? err.message : "Something went wrong.";
}

/**
 * One sentence about the open mail becomes one proposed change to the
 * rules -- any number of rules added, changed, moved or removed -- which is
 * accepted or declined as a whole. Once an answer is shown the sentence
 * stays editable, and Run again asks afresh with what it says now: a new,
 * independent request that carries nothing over from the previous one.
 * Closing the dialog by any route aborts a request still running, and the
 * server stops before its next model call.
 */
export function AddRuleDialog({ mailId, open, onOpenChange }: AddRuleDialogProps) {
  const qc = useQueryClient();
  const { push: pushToast } = useToast();
  const [prompt, setPrompt] = useState("");
  const [view, setView] = useState<View>({ kind: "prompt" });
  const [accepting, setAccepting] = useState(false);
  // Set synchronously: `view`/`accepting` are render snapshots, so two
  // fast presses reaching the handler would both read them as idle.
  const busyRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []);

  function handleOpenChange(next: boolean) {
    if (!next) {
      abortRef.current?.abort();
      abortRef.current = null;
      busyRef.current = false;
      setAccepting(false);
      setPrompt("");
      setView({ kind: "prompt" });
    }
    onOpenChange(next);
  }

  async function send() {
    const text = prompt.trim();
    if (!text || busyRef.current) return;
    busyRef.current = true;
    const controller = new AbortController();
    abortRef.current = controller;
    setView({ kind: "running" });
    try {
      const response = await api.pipeline.assistant(
        { message_id: mailId, prompt: text },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setView(
        response.change
          ? { kind: "result", response, change: response.change }
          : { kind: "empty", message: response.message },
      );
    } catch (err) {
      if (isAbort(err) || controller.signal.aborted) return;
      setView({ kind: "error", message: describeError(err) });
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      busyRef.current = false;
    }
  }

  async function accept(change: RuleAssistantChange) {
    if (busyRef.current) return;
    busyRef.current = true;
    setAccepting(true);
    try {
      await api.pipeline.replace({
        base_revision: change.base_revision,
        enabled: change.enabled,
        stages: change.stages as unknown as Record<string, unknown>[],
      });
      qc.invalidateQueries({ queryKey: pipelineKeys.document });
      qc.invalidateQueries({ queryKey: pipelineKeys.health });
      pushToast("Rules saved", "success");
      handleOpenChange(false);
    } catch (err) {
      setView({
        kind: "error",
        message:
          err instanceof ApiError && err.status === 409
            ? "Rules changed meanwhile — ask again."
            : describeError(err),
      });
    } finally {
      busyRef.current = false;
      setAccepting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent size="lg" className="max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Add rule</DialogTitle>
        </DialogHeader>

        {view.kind === "prompt" && (
          <div className="flex flex-col gap-3">
            <p className="text-sm text-muted-foreground">
              Say in a sentence what should happen to mail like the one you have open. The
              assistant may add, change, reorder or remove several rules at once.
            </p>
            <PromptField autoFocus value={prompt} onChange={setPrompt} onSubmit={() => void send()} />
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => handleOpenChange(false)}>
                Cancel
              </Button>
              <Button disabled={!prompt.trim()} onClick={() => void send()}>
                Propose change
              </Button>
            </div>
          </div>
        )}

        {view.kind === "running" && (
          <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Working out a change…
          </div>
        )}

        {(view.kind === "result" || view.kind === "empty" || view.kind === "error") && (
          <div className="flex flex-col gap-2 border-b pb-3">
            <PromptField value={prompt} onChange={setPrompt} onSubmit={() => void send()} />
            <div className="flex justify-end">
              <Button
                variant="outline"
                size="sm"
                disabled={!prompt.trim() || accepting}
                onClick={() => void send()}
              >
                Run again
              </Button>
            </div>
          </div>
        )}

        {view.kind === "result" && (
          <ProposalView
            response={view.response}
            change={view.change}
            accepting={accepting}
            onAccept={() => void accept(view.change)}
            onDecline={() => handleOpenChange(false)}
          />
        )}

        {(view.kind === "empty" || view.kind === "error") && (
          <div className="flex flex-col gap-3">
            <p
              className={view.kind === "error" ? "text-sm text-destructive" : "text-sm"}
              role={view.kind === "error" ? "alert" : undefined}
            >
              {view.message}
            </p>
            <div className="flex justify-end">
              <Button variant="outline" onClick={() => handleOpenChange(false)}>
                Close
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function PromptField({
  value,
  onChange,
  onSubmit,
  autoFocus,
}: {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  autoFocus?: boolean;
}) {
  return (
    <Textarea
      autoFocus={autoFocus}
      value={value}
      maxLength={MAX_PROMPT_CHARS}
      placeholder="These should go to Newsletter too"
      aria-label="What should the rule do?"
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => {
        if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
        e.preventDefault();
        onSubmit();
      }}
    />
  );
}

function ProposalView({
  response,
  change,
  accepting,
  onAccept,
  onDecline,
}: {
  response: RuleAssistantResponse;
  change: RuleAssistantChange;
  accepting: boolean;
  onAccept: () => void;
  onDecline: () => void;
}) {
  const { preview } = response;
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <p className="text-sm">{response.message}</p>
      <h3 className="text-sm font-medium">{change.title}</h3>

      {change.rules.map((rule) => (
        <RuleChangeView key={`${rule.kind}-${rule.stage_id}`} rule={rule} />
      ))}

      {preview && (
        <div className="text-sm">
          <p>
            Would have caught {preview.matched_after} of your last {preview.sample_size} mails
            (now: {preview.matched_before})
          </p>
          {preview.examples.length > 0 && (
            <ul className="mt-1 list-disc pl-5 text-muted-foreground">
              {preview.examples.map((example, i) => (
                <li key={i} className="break-words">
                  {example.from_addr} — {example.subject}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {response.warnings.map((warning) => (
        <p key={warning} className="text-sm text-amber-600 dark:text-amber-400" role="status">
          {warning}
        </p>
      ))}

      <div className="flex justify-end gap-2">
        <Button variant="outline" disabled={accepting} onClick={onDecline}>
          Decline
        </Button>
        <Button disabled={accepting} onClick={onAccept}>
          {accepting && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
          Accept
        </Button>
      </div>
    </div>
  );
}

const KIND_LABEL: Record<RuleAssistantRuleChange["kind"], string> = {
  added: "New",
  changed: "Changed",
  moved: "Moved",
  removed: "Removed",
};

function RuleChangeView({ rule }: { rule: RuleAssistantRuleChange }) {
  return (
    <section className="flex min-w-0 flex-col gap-2 rounded-md border p-2">
      <p className="text-sm">
        <span className="font-medium">{KIND_LABEL[rule.kind]}</span> — {rule.name}
      </p>
      {rule.kind === "moved" ? (
        <p className="text-xs text-muted-foreground">
          Only its place in the rule order changes; the rule itself stays as it is.
        </p>
      ) : (
        <DiffView rows={rule.diff} />
      )}
    </section>
  );
}

const ROW_STYLE: Record<DiffRow["op"], { sign: string; row: string; mark: string }> = {
  added: {
    sign: "+",
    row: "bg-green-500/15 text-green-900 dark:text-green-200",
    mark: "rounded-sm bg-green-500/40",
  },
  removed: {
    sign: "−",
    row: "bg-red-500/15 text-red-900 dark:text-red-200",
    mark: "rounded-sm bg-red-500/40",
  },
  context: { sign: " ", row: "text-muted-foreground", mark: "" },
  gap: { sign: "", row: "", mark: "" },
};

/** The rule's JSON as a diff: removed lines red, added green, the changed
 * characters inside a replaced line marked, and long unchanged stretches
 * folded into a count. */
function DiffView({ rows }: { rows: DiffRow[] }) {
  return (
    <div
      data-testid="rule-diff"
      className="max-h-80 overflow-auto rounded-md border bg-muted/40 py-1 font-mono text-xs"
    >
      {rows.map((row, i) => {
        if (row.op === "gap") {
          return (
            <div key={i} className="px-2 py-0.5 text-muted-foreground/70 select-none">
              ⋯ {row.skipped} unchanged line{row.skipped === 1 ? "" : "s"}
            </div>
          );
        }
        const style = ROW_STYLE[row.op];
        return (
          <div
            key={i}
            data-op={row.op}
            className={`flex gap-2 px-2 whitespace-pre-wrap break-all ${style.row}`}
          >
            <span aria-hidden className="w-2 shrink-0 select-none">
              {style.sign}
            </span>
            <span className="min-w-0">
              {row.segments.map((segment, j) =>
                segment.changed ? (
                  <mark key={j} className={`text-inherit ${style.mark}`}>
                    {segment.text}
                  </mark>
                ) : (
                  <span key={j}>{segment.text}</span>
                ),
              )}
            </span>
          </div>
        );
      })}
    </div>
  );
}
