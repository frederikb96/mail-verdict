"use client";

/**
 * Every keyboard shortcut, grouped by where it acts, with a way to record a
 * different chord for each rebindable one, switch it off, or go back to its
 * default. Bindings are stored per browser (lib/shortcut-bindings.ts).
 */

import { useEffect, useState } from "react";
import { useAtom } from "jotai";
import { Keyboard, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ClientOnly } from "@/components/client-only";
import { ShortcutKeys } from "@/components/layout/shortcuts-overlay";
import { shortcutOverridesAtom } from "@/lib/shortcut-bindings";
import {
  FIXED_SHORTCUTS,
  SCOPE_LABELS,
  SCOPE_ORDER,
  SHORTCUT_ACTIONS,
  chordFromEvent,
  chordsEqual,
  effectiveBindings,
  findConflict,
  formatChord,
  reservedReason,
  shortcutAction,
  withOverride,
  type ShortcutActionId,
  type ShortcutChord,
} from "@/lib/shortcuts";

interface Recording {
  id: ShortcutActionId;
  /** Set once a chord was pressed that another action already uses. */
  pending?: { chord: ShortcutChord; conflict: ShortcutActionId };
  error?: string;
}

function ShortcutsEditor() {
  const [overrides, setOverrides] = useAtom(shortcutOverridesAtom);
  const bindings = effectiveBindings(overrides);
  const [recording, setRecording] = useState<Recording | null>(null);
  const recordingId = recording?.id ?? null;
  const awaitingKeys = recording !== null && !recording.pending;

  // Capture phase on window runs before every other shortcut listener, so
  // the keys being recorded never also trigger what they are bound to now.
  useEffect(() => {
    if (!awaitingKeys || !recordingId) return;
    const onKeyDown = (e: KeyboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.repeat) return;
      if (e.key === "Escape" && !e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey) {
        setRecording(null);
        return;
      }
      const next = chordFromEvent(e);
      if (!next) return;
      const reason = reservedReason(next);
      if (reason) {
        setRecording({ id: recordingId, error: `${formatChord(next)}: ${reason}` });
        return;
      }
      const conflict = findConflict(next, recordingId, bindings);
      if (conflict) {
        setRecording({ id: recordingId, pending: { chord: next, conflict } });
        return;
      }
      setOverrides(withOverride(overrides, recordingId, next));
      setRecording(null);
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [awaitingKeys, recordingId, bindings, overrides, setOverrides]);

  const replaceConflict = () => {
    if (!recording?.pending) return;
    const { chord, conflict } = recording.pending;
    setOverrides(withOverride(withOverride(overrides, conflict, null), recording.id, chord));
    setRecording(null);
  };

  const changed = Object.keys(overrides).length > 0;

  return (
    <div className="flex flex-col gap-6 p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h1 className="flex items-center gap-2 text-2xl font-semibold">
            <Keyboard className="h-6 w-6" />
            Keyboard shortcuts
          </h1>
          <p className="max-w-2xl text-sm text-muted-foreground">
            Letters follow what is printed on your keyboard, whatever its layout: <ShortcutKeys>z</ShortcutKeys>{" "}
            is the key labelled Z on a German keyboard as much as on a US one. Changes apply at once and
            are kept in this browser.
            {bindings.show_shortcuts && (
              <>
                {" "}
                <ShortcutKeys>{formatChord(bindings.show_shortcuts)}</ShortcutKeys> shows them all from any page.
              </>
            )}
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          disabled={!changed}
          onClick={() => {
            setOverrides({});
            setRecording(null);
          }}
        >
          <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
          Reset all
        </Button>
      </div>

      {SCOPE_ORDER.map((scope) => (
        <Card key={scope}>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">{SCOPE_LABELS[scope]}</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col">
            {SHORTCUT_ACTIONS.filter((a) => a.scope === scope).map((action) => {
              const bound = bindings[action.id];
              const isDefault = bound !== null && chordsEqual(bound, action.default);
              const active = recording?.id === action.id ? recording : null;
              return (
                <div
                  key={action.id}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b py-2 last:border-b-0"
                >
                  <span className="min-w-0 flex-1 text-sm">{action.label}</span>
                  {active && !active.pending ? (
                    <span className="rounded border border-primary bg-primary/10 px-2 py-0.5 text-xs">
                      Press the new keys… <span className="text-muted-foreground">(Esc cancels)</span>
                    </span>
                  ) : bound ? (
                    <ShortcutKeys>{formatChord(bound)}</ShortcutKeys>
                  ) : (
                    <span className="text-xs text-muted-foreground">Off</span>
                  )}
                  <div className="flex items-center gap-1">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 px-2 text-xs"
                      onClick={() => setRecording(active ? null : { id: action.id })}
                    >
                      {active ? "Cancel" : "Change"}
                    </Button>
                    {bound && !active && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 px-2 text-xs"
                        onClick={() => setOverrides(withOverride(overrides, action.id, null))}
                      >
                        Turn off
                      </Button>
                    )}
                    {!isDefault && !active && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 px-2 text-xs"
                        title={`Default: ${formatChord(action.default)}`}
                        onClick={() => setOverrides(withOverride(overrides, action.id, action.default))}
                      >
                        Default ({formatChord(action.default)})
                      </Button>
                    )}
                  </div>
                  {active?.error && (
                    <p className="basis-full text-xs text-destructive">{active.error} Try another.</p>
                  )}
                  {active?.pending && (
                    <div className="flex basis-full flex-wrap items-center gap-2 text-xs">
                      <span>
                        <ShortcutKeys>{formatChord(active.pending.chord)}</ShortcutKeys> already does
                        “{shortcutAction(active.pending.conflict).label}”.
                      </span>
                      <Button size="sm" className="h-6 px-2 text-xs" onClick={replaceConflict}>
                        Use it here, turn that one off
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-6 px-2 text-xs"
                        onClick={() => setRecording({ id: action.id })}
                      >
                        Try another
                      </Button>
                    </div>
                  )}
                </div>
              );
            })}
            {FIXED_SHORTCUTS[scope].map((fixed) => (
              <div
                key={`${fixed.label}|${fixed.keys}`}
                className="flex items-center gap-3 border-b py-2 text-muted-foreground last:border-b-0"
              >
                <span className="min-w-0 flex-1 text-sm">{fixed.label}</span>
                <ShortcutKeys>{fixed.keys}</ShortcutKeys>
                <span className="w-[4.5rem] text-right text-xs">fixed</span>
              </div>
            ))}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

export function ShortcutsPage() {
  return (
    <ClientOnly>
      <ShortcutsEditor />
    </ClientOnly>
  );
}
