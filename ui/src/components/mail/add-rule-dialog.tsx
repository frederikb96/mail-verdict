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
import type { RuleAssistantChange, RuleAssistantResponse } from "@/types/api";

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
 * rules, which is accepted or declined -- no history, no second prompt.
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
    const { stage } = change;
    try {
      if (change.is_new) {
        await api.pipeline.createStage({
          stage_id: stage.stage_id,
          type: stage.type,
          name: stage.name,
          config: stage.config,
          enabled: stage.enabled,
          halt: stage.halt,
          accounts: stage.accounts,
          base_revision: change.base_revision,
        });
      } else {
        await api.pipeline.updateStage(stage.stage_id, {
          name: stage.name,
          config: stage.config,
          halt: stage.halt,
          base_revision: change.base_revision,
        });
      }
      qc.invalidateQueries({ queryKey: pipelineKeys.document });
      qc.invalidateQueries({ queryKey: pipelineKeys.health });
      pushToast("Rule saved", "success");
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
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Add rule</DialogTitle>
        </DialogHeader>

        {view.kind === "prompt" && (
          <div className="flex flex-col gap-3">
            <p className="text-sm text-muted-foreground">
              Say in one sentence what should happen to mail like the one you have open.
            </p>
            <Textarea
              autoFocus
              value={prompt}
              maxLength={MAX_PROMPT_CHARS}
              placeholder="These should go to Newsletter too"
              aria-label="What should the rule do?"
              onChange={(e) => setPrompt(e.target.value)}
              onKeyDown={(e) => {
                if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
                e.preventDefault();
                void send();
              }}
            />
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => handleOpenChange(false)}>
                Cancel
              </Button>
              <Button disabled={!prompt.trim()} onClick={() => void send()}>
                Propose rule
              </Button>
            </div>
          </div>
        )}

        {view.kind === "running" && (
          <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Working out a rule…
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

      {change.before_text !== null && <CodeBlock label="Now" text={change.before_text} />}
      <CodeBlock label="Proposed" text={change.after_text} />
      {change.effects_text !== null && (
        <CodeBlock label="What this rule does" text={change.effects_text} />
      )}

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

function CodeBlock({ label, text }: { label: string; text: string }) {
  return (
    <div>
      <p className="mb-1 text-xs font-medium text-muted-foreground">{label}</p>
      <pre className="max-h-48 overflow-auto rounded-md bg-muted p-2 font-mono text-xs whitespace-pre-wrap break-words">
        {text}
      </pre>
    </div>
  );
}
