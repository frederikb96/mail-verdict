/**
 * Keyboard shortcuts for mail navigation and actions.
 *
 * Web-only; disabled when an input/textarea is focused.
 */

"use client";

import { useEffect, useCallback } from "react";
import { useAtom, useAtomValue, useSetAtom } from "jotai";
import { focusedMailIndexAtom } from "@/store/focused-mail-atom";
import {
  composeIntentAtom,
  nextBulkRequestNonce,
  requestBulkMoveMenuAtom,
  requestBulkQuickActionAtom,
  requestMoveDialogAtom,
  requestReplyModeAtom,
  requestSelectMailAtom,
} from "@/lib/atoms";
import { useClearSelection, useSelection, useSelectionGestures } from "@/hooks/use-selection";
import { useUndoMailAction } from "@/hooks/use-mail-intents";
import { isEditableElement } from "@/lib/utils";
import { isRowUnread } from "@/lib/mail-unread";
import type { MailRowAction, MessageSummary } from "@/types/api";

interface UseKeyboardShortcutsOptions {
  /** Current visible mail list. */
  mails: MessageSummary[];
  /** The row standing for the open message -- see MailList's openRowId. */
  openMailId: string | null;
  /** Callback to scroll the VList to a given index. */
  scrollToIndex?: (index: number) => void;
  /** Opens a message, the same way clicking its row does. */
  onOpen: (mailId: string) => void;
  /** Runs an action on a message, the same way its row control does. */
  onAction: (mailId: string, action: MailRowAction, accountId?: string) => void;
}

/** Whether an undo keystroke belongs to something other than the mail
 * actions: text being edited, a dialog, or a composer anywhere on the page
 * -- someone who clicked out of a reply to press Ctrl+Z still means its
 * text. */
function ownsUndo(target: EventTarget | null): boolean {
  if (isEditableElement(target)) return true;
  if (
    target instanceof Element &&
    target.closest('[role="dialog"], [role="alertdialog"]') !== null
  ) {
    return true;
  }
  return document.querySelector('[data-slot="compose-form"]') !== null;
}

/** Ctrl+Z, or Cmd+Z on macOS -- shift held is redo, and left alone. */
function isUndoKey(e: KeyboardEvent): boolean {
  return (e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "z";
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
 * - r: mark as read (toggle read/unread outside a multi-selection)
 * - u: mark as unread
 * - s: star
 * - c: compose a new message
 * - a: reply all (the open message only -- there is no row form of this)
 * - f: forward (the open message only)
 * - v: move to folder
 * - Ctrl+Z / Cmd+Z: undo this tab's most recent mail action, repeatedly --
 *   never while typing, with a composer open, or in a dialog, where the
 *   browser's own undo belongs
 *
 * Every one of them acts on the open message when there is one, and on the
 * focused row otherwise -- so a shortcut and a click on the same message's
 * own control do the same thing, auto-advance included. With more than one
 * message ticked -- the same threshold the reading pane itself switches to
 * the bulk panel at -- e/Delete/!/r/u/s/v act on the whole selection
 * instead, through the same bulk-action request the toolbar's own buttons
 * send, and clear the selection the same way those do. r and s have no
 * single message to read a toggle target from there, so they always mean
 * "mark read" and "star" -- the two actions the bulk panel itself offers a
 * button for. c/a/f are the exception: compose, reply and forward have
 * nothing to do with a selection, and reply/forward exist only for an open
 * message (there is no row form of either), so they act only while the
 * reading pane actually has one open and no multi-selection is shadowing
 * it.
 *
 * `/` (focus the global search field) and `?` (the shortcuts overlay) are
 * not here -- they are not specific to a mail list, so they are registered
 * once, globally, in components/layout/shortcuts-overlay.tsx and
 * app-header.tsx respectively.
 */
export function useKeyboardShortcuts({
  mails,
  openMailId,
  scrollToIndex,
  onOpen,
  onAction,
}: UseKeyboardShortcutsOptions) {
  const [focusedIndex, setFocusedIndex] = useAtom(focusedMailIndexAtom);
  const requestSelectMail = useSetAtom(requestSelectMailAtom);
  const { toggle: toggleSelection } = useSelectionGestures();
  const clearSelection = useClearSelection();
  const setComposeIntent = useSetAtom(composeIntentAtom);
  const setRequestReplyMode = useSetAtom(requestReplyModeAtom);
  const setRequestMoveDialog = useSetAtom(requestMoveDialogAtom);
  const setRequestBulkMoveMenu = useSetAtom(requestBulkMoveMenuAtom);
  const setRequestBulkQuickAction = useSetAtom(requestBulkQuickActionAtom);
  const undoMailAction = useUndoMailAction();
  // Same threshold reading-pane.tsx switches the pane to the bulk panel
  // at -- below it a lone ticked row is not "a selection" for shortcut
  // purposes, and the open/focused message is still what e/r/s/etc. act
  // on.
  const { count: selectionCount } = useSelection();
  const multiSelect = selectionCount > 1;

  const openIndex = openMailId
    ? mails.findIndex((m) => m.id === openMailId)
    : -1;
  const currentIndex = openIndex >= 0 ? openIndex : focusedIndex;

  const getCurrentMail = useCallback((): MessageSummary | null => {
    if (currentIndex < 0 || currentIndex >= mails.length) return null;
    return mails[currentIndex];
  }, [currentIndex, mails]);

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (isUndoKey(e)) {
        // Holding the keys down must not walk back the whole stack.
        if (e.repeat || ownsUndo(e.target)) return;
        if (undoMailAction()) e.preventDefault();
        return;
      }
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

      // Acts on the whole ticked selection through the same request the
      // bulk panel's own buttons send -- see requestBulkQuickActionAtom --
      // so a destructive action over a "select all" predicate still
      // confirms with a count, and the selection clears itself the same
      // way a button click does. Otherwise acts on the open/focused
      // message exactly as before; getCurrentMail() is never consulted
      // once a multi-selection is driving.
      function act(action: MailRowAction) {
        if (multiSelect) {
          setRequestBulkQuickAction({ action, nonce: nextBulkRequestNonce() });
          return;
        }
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
          // No single message to read a toggle target from once several
          // are ticked -- "mark read" is what the bulk panel itself offers
          // a button for, "mark unread" stays reachable through u.
          if (multiSelect) {
            act("mark_read");
            break;
          }
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
          // Same reasoning as r -- and the bulk panel itself offers only
          // "Star", never an "Unstar", over a selection.
          if (multiSelect) {
            act("flag");
            break;
          }
          const mail = getCurrentMail();
          if (mail) act(mail.is_flagged ? "unflag" : "flag");
          break;
        }
        case "c": {
          e.preventDefault();
          setComposeIntent({});
          break;
        }
        case "a": {
          // Acts on the open reading pane, not merely a focused row --
          // openIndex, not currentIndex, is "something is actually open".
          // A multi-selection replaces that pane with the bulk panel, so
          // there is nothing here to reply to even when openIndex still
          // names a message.
          if (openIndex < 0 || multiSelect) return;
          e.preventDefault();
          setRequestReplyMode({ mode: "reply-all", nonce: Date.now() });
          break;
        }
        case "f": {
          if (openIndex < 0 || multiSelect) return;
          e.preventDefault();
          setRequestReplyMode({ mode: "forward", nonce: Date.now() });
          break;
        }
        case "v": {
          if (multiSelect) {
            e.preventDefault();
            setRequestBulkMoveMenu({ nonce: nextBulkRequestNonce() });
            break;
          }
          if (openIndex < 0) return;
          e.preventDefault();
          setRequestMoveDialog({ nonce: Date.now() });
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
    setComposeIntent,
    setRequestReplyMode,
    setRequestMoveDialog,
    setRequestBulkMoveMenu,
    setRequestBulkQuickAction,
    onAction,
    toggleSelection,
    clearSelection,
    scrollToIndex,
    undoMailAction,
    multiSelect,
  ]);
}
