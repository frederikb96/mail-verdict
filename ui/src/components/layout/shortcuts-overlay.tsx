"use client";

/**
 * The cheat sheet -- every keyboard shortcut in the app, as this browser
 * has them bound, opened by its own shortcut (`?` by default) from any
 * page. Read from the registry (lib/shortcuts.ts), so a rebinding on the
 * shortcuts page shows here at once.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { Keyboard } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { isEditableElement } from "@/lib/utils";
import { useShortcutBindings } from "@/lib/shortcut-bindings";
import {
  FIXED_SHORTCUTS,
  SCOPE_LABELS,
  SCOPE_ORDER,
  SHORTCUT_ACTIONS,
  formatChord,
  matchAction,
} from "@/lib/shortcuts";

export function ShortcutKeys({ children }: { children: React.ReactNode }) {
  return (
    <kbd className="whitespace-nowrap rounded border bg-muted px-1.5 py-0.5 font-mono text-xs">
      {children}
    </kbd>
  );
}

export function ShortcutsOverlay() {
  const [open, setOpen] = useState(false);
  const bindings = useShortcutBindings();

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (matchAction(e, bindings, "global") !== "show_shortcuts") return;
      if (isEditableElement(e.target)) return;
      e.preventDefault();
      setOpen((o) => !o);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [bindings]);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>Keyboard shortcuts</DialogTitle>
        </DialogHeader>
        <div className="grid gap-x-8 gap-y-5 sm:grid-cols-2">
          {SCOPE_ORDER.map((scope) => {
            const items = [
              ...SHORTCUT_ACTIONS.filter((a) => a.scope === scope && bindings[a.id]).map((a) => ({
                keys: formatChord(bindings[a.id]!),
                label: a.label,
              })),
              ...FIXED_SHORTCUTS[scope],
            ];
            if (items.length === 0) return null;
            return (
              <div key={scope}>
                <h3 className="mb-1.5 text-xs font-semibold text-muted-foreground">
                  {SCOPE_LABELS[scope]}
                </h3>
                <dl className="space-y-1">
                  {items.map((item) => (
                    <div key={`${item.label}|${item.keys}`} className="flex items-center justify-between gap-3 text-sm">
                      <dt className="text-muted-foreground">{item.label}</dt>
                      <dd>
                        <ShortcutKeys>{item.keys}</ShortcutKeys>
                      </dd>
                    </div>
                  ))}
                </dl>
              </div>
            );
          })}
        </div>
        <Link
          href="/shortcuts"
          onClick={() => setOpen(false)}
          className="flex items-center gap-1.5 text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
        >
          <Keyboard className="h-4 w-4" />
          Change shortcuts
        </Link>
      </DialogContent>
    </Dialog>
  );
}
