/**
 * Keyboard shortcuts for mail navigation and actions.
 *
 * Web-only; disabled when an input/textarea is focused.
 */

"use client";

import { useEffect, useCallback } from "react";
import { useAtom, useAtomValue, useSetAtom } from "jotai";
import { focusedMailIndexAtom } from "@/store/focused-mail-atom";
import { selectedMailIdAtom, requestSelectMailAtom } from "@/lib/atoms";
import { useClearSelection, useSelectionGestures } from "@/hooks/use-selection";
import { isEditableElement } from "@/lib/utils";
import { isRowUnread } from "@/lib/mail-unread";
import type { MailRowAction, MessageSummary } from "@/types/api";

interface UseKeyboardShortcutsOptions {
  /** Current visible mail list. */
  mails: MessageSummary[];
  /** Callback to scroll the VList to a given index. */
  scrollToIndex?: (index: number) => void;
  /** Opens a message, the same way clicking its row does. */
  onOpen: (mailId: string) => void;
  /** Runs an action on a message, the same way its row control does. */
  onAction: (mailId: string, action: MailRowAction, accountId?: string) => void;
}

/**
 * Registers global keyboard shortcuts for mail navigation and actions.
 *
 * Shortcuts:
 * - ArrowDown/j: move to the next (older) message
 * - ArrowUp/k: move to the previous (newer) message
 * - Enter: open the focused message
 * - Escape: close the reading pane / clear selection
 * - x: toggle checkbox selection on the current message
 * - e: archive
 * - Delete/#: move to trash
 * - !: mark as spam
 * - r: toggle read/unread
 * - u: mark as unread
 * - s: toggle star
 *
 * Every one of them acts on the open message when there is one, and on the
 * focused row otherwise -- so a shortcut and a click on the same message's
 * own control do the same thing, auto-advance included.
 */
export function useKeyboardShortcuts({
  mails,
  scrollToIndex,
  onOpen,
  onAction,
}: UseKeyboardShortcutsOptions) {
  const [focusedIndex, setFocusedIndex] = useAtom(focusedMailIndexAtom);
  const selectedMailId = useAtomValue(selectedMailIdAtom);
  const requestSelectMail = useSetAtom(requestSelectMailAtom);
  const { toggle: toggleSelection } = useSelectionGestures();
  const clearSelection = useClearSelection();

  const openIndex = selectedMailId
    ? mails.findIndex((m) => m.id === selectedMailId)
    : -1;
  const currentIndex = openIndex >= 0 ? openIndex : focusedIndex;

  const getCurrentMail = useCallback((): MessageSummary | null => {
    if (currentIndex < 0 || currentIndex >= mails.length) return null;
    return mails[currentIndex];
  }, [currentIndex, mails]);

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (isEditableElement(e.target)) return;
      // A shortcut is a bare keypress: ctrl+r reloads the page and cmd+e
      // belongs to the browser, so neither may be swallowed here.
      if (e.ctrlKey || e.metaKey || e.altKey) return;

      // Moving between messages carries the reading pane with it whenever
      // one is open, so arrow keys walk the list the way the reader
      // already reads it.
      function move(delta: number) {
        if (mails.length === 0) return;
        const from = currentIndex < 0 ? (delta > 0 ? -1 : mails.length) : currentIndex;
        const next = Math.max(0, Math.min(from + delta, mails.length - 1));
        setFocusedIndex(next);
        scrollToIndex?.(next);
        if (openIndex >= 0) onOpen(mails[next].id);
      }

      function act(action: MailRowAction) {
        const mail = getCurrentMail();
        if (mail) onAction(mail.id, action, mail.account_id);
      }

      switch (e.key) {
        case "ArrowDown":
        case "j": {
          if (currentIndex < 0 && e.key === "ArrowDown") return; // let the page scroll
          e.preventDefault();
          move(1);
          break;
        }
        case "ArrowUp":
        case "k": {
          if (currentIndex < 0 && e.key === "ArrowUp") return;
          e.preventDefault();
          move(-1);
          break;
        }
        case "Enter": {
          const mail = getCurrentMail();
          if (!mail) return;
          e.preventDefault();
          onOpen(mail.id);
          break;
        }
        case "Escape": {
          e.preventDefault();
          requestSelectMail(null);
          clearSelection();
          break;
        }
        case "x": {
          const mail = getCurrentMail();
          if (!mail) return;
          e.preventDefault();
          toggleSelection(mail);
          break;
        }
        case "e": {
          e.preventDefault();
          act("archive");
          break;
        }
        case "Delete":
        case "#": {
          e.preventDefault();
          act("trash");
          break;
        }
        case "!": {
          e.preventDefault();
          act("spam");
          break;
        }
        case "r": {
          e.preventDefault();
          const mail = getCurrentMail();
          if (mail) act(isRowUnread(mail) ? "mark_read" : "mark_unread");
          break;
        }
        case "u": {
          e.preventDefault();
          act("mark_unread");
          break;
        }
        case "s": {
          e.preventDefault();
          const mail = getCurrentMail();
          if (mail) act(mail.is_flagged ? "unflag" : "flag");
          break;
        }
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [
    currentIndex,
    openIndex,
    mails,
    setFocusedIndex,
    requestSelectMail,
    getCurrentMail,
    onOpen,
    onAction,
    toggleSelection,
    clearSelection,
    scrollToIndex,
  ]);
}
