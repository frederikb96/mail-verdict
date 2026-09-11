"use client";

/**
 * The `?` cheat sheet -- every keyboard shortcut in the app, in one place,
 * since none of them were discoverable anywhere in the interface before
 * this existed. Mounted once, globally (like section-shortcuts.tsx),
 * rather than scoped to the mail list the way most of the shortcuts it
 * lists are: it needs to open from any page, and it lists the calendar's
 * and the section jumps too.
 *
 * A static list rather than one derived from the hooks that implement
 * each shortcut -- those are scattered across use-keyboard-shortcuts.ts,
 * use-calendar-shortcuts.ts and section-shortcuts.tsx, each keyed
 * differently (KeyboardEvent.key vs .code) for reasons specific to that
 * hook, and none of them expose a list a UI could render. Keeping this in
 * sync by hand costs less than building a registry three call sites would
 * have to route through just to feed one dialog.
 */

import { useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { isEditableElement } from "@/lib/utils";

interface ShortcutGroup {
  title: string;
  items: Array<{ keys: string; description: string }>;
}

const GROUPS: ShortcutGroup[] = [
  {
    title: "Mail",
    items: [
      { keys: "j / ↓", description: "Next message" },
      { keys: "k / ↑", description: "Previous message" },
      { keys: "Enter", description: "Open the focused message" },
      { keys: "Esc", description: "Close the reading pane / clear selection" },
      { keys: "x", description: "Toggle selection on the current message" },
      { keys: "c", description: "Compose a new message" },
      { keys: "a", description: "Reply all" },
      { keys: "f", description: "Forward" },
      { keys: "v", description: "Move to folder" },
      { keys: "e", description: "Archive" },
      { keys: "Delete / #", description: "Move to trash" },
      { keys: "!", description: "Mark as spam" },
      { keys: "r", description: "Toggle read / unread" },
      { keys: "u", description: "Mark as unread" },
      { keys: "s", description: "Toggle star" },
      { keys: "/", description: "Focus search" },
    ],
  },
  {
    title: "Calendar",
    items: [
      { keys: "t", description: "Jump to today" },
      { keys: "d", description: "Day view" },
      { keys: "w", description: "Week view" },
      { keys: "m", description: "Month view" },
      { keys: "a", description: "Agenda view" },
      { keys: "n", description: "New event" },
      { keys: "j / k / ← / →", description: "Next / previous period" },
      { keys: "Delete", description: "Delete the selected event" },
      { keys: "Esc", description: "Close the event popover" },
    ],
  },
  {
    title: "Sections",
    items: [
      { keys: "Ctrl+Shift+1", description: "Mail" },
      { keys: "Ctrl+Shift+2", description: "Calendar" },
      { keys: "Ctrl+Shift+3", description: "Contacts" },
    ],
  },
];

export function ShortcutsOverlay() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key !== "?" || e.ctrlKey || e.metaKey || e.altKey) return;
      if (isEditableElement(e.target)) return;
      e.preventDefault();
      setOpen((o) => !o);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Keyboard shortcuts</DialogTitle>
        </DialogHeader>
        <div className="grid gap-4 sm:grid-cols-2">
          {GROUPS.map((group) => (
            <div key={group.title}>
              <h3 className="mb-1.5 text-xs font-semibold text-muted-foreground">
                {group.title}
              </h3>
              <dl className="space-y-1">
                {group.items.map((item) => (
                  <div key={item.keys} className="flex items-center justify-between gap-3 text-sm">
                    <dt className="text-muted-foreground">{item.description}</dt>
                    <dd>
                      <kbd className="rounded border bg-muted px-1.5 py-0.5 font-mono text-xs">
                        {item.keys}
                      </kbd>
                    </dd>
                  </div>
                ))}
              </dl>
            </div>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  );
}
