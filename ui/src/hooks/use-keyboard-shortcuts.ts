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
import { useShortcutBindings } from "@/lib/shortcut-bindings";
import { matchAction } from "@/lib/shortcuts";
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

function isInsideDialog(target: EventTarget | null): boolean {
  return (
    target instanceof Element &&
    target.closest('[role="dialog"], [role="alertdialog"]') !== null
  );
}

/** Whether an undo keystroke belongs to something other than the mail
 * actions: text being edited, a dialog, or a composer anywhere on the page
 * -- someone who clicked out of a reply to press Ctrl+Z still means its
 * text. */
function ownsUndo(target: EventTarget | null): boolean {
  if (isEditableElement(target)) return true;
  if (isInsideDialog(target)) return true;
  return document.querySelector('[data-slot="compose-form"]') !== null;
}

/** Ctrl+Z, or Cmd+Z on macOS -- shift held is redo, and left alone. */
function isUndoKey(e: KeyboardEvent): boolean {
  return (e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "z";
}

/**
 * Registers the mail list's keyboard shortcuts: the rebindable actions of
 * the "mail" scope in lib/shortcuts.ts (defaults: j/k move, x select, c
 * compose, Shift+R reply, a reply all, f forward, v move, e archive, #
 * trash, ! spam, r toggle read, u unread, s star), plus the fixed keys --
 * arrows, Enter, Escape, Delete and Ctrl+Z / Cmd+Z, which undoes this
 * tab's most recent mail action, repeatedly, never while typing, with a
 * composer open, or in a dialog, where the browser's own undo belongs.
 *
 * Every one of them acts on the open message when there is one, and on the
 * focused row otherwise -- so a shortcut and a click on the same message's
 * own control do the same thing, auto-advance included. With more than one
 * message ticked -- the same threshold the reading pane itself switches to
 * the bulk panel at -- archive, trash, spam, read, unread, star and move act
 * on the whole selection instead, through the same bulk-action request the
 * toolbar's own buttons send, and clear the selection the same way those
 * do. Toggle read and star have no single message to read a toggle target
 * from there, so they always mean "mark read" and "star" -- the two actions
 * the bulk panel itself offers a button for. Compose, reply, reply all and
 * forward are the exception: they have nothing to do with a selection, and
 * the replies exist only for an open message (there is no row form of
 * them), so they act only while the reading pane actually has one open and
 * no multi-selection is shadowing it.
 *
 * Focusing the search field and the cheat sheet are not here -- they are
 * not specific to a mail list (components/layout/app-header.tsx and
 * shortcuts-overlay.tsx).
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
  const bindings = useShortcutBindings();
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
      // A dialog's own buttons and fields own the keyboard: without this,
      // `e` or Delete with Accept focused would archive or trash the mail
      // the dialog is about, and Enter would be swallowed.
      if (isInsideDialog(e.target)) return;
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

      // The fixed keys are bare presses: ctrl+Enter or shift+Delete
      // belong to someone else.
      const bare = !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey;
      const fixed = bare ? e.key : null;
      switch (fixed) {
        case "ArrowDown": {
          if (currentIndex < 0) return; // let the page scroll
          e.preventDefault();
          move(1);
          return;
        }
        case "ArrowUp": {
          if (currentIndex < 0) return;
          e.preventDefault();
          move(-1);
          return;
        }
        case "Enter": {
          const mail = getCurrentMail();
          if (!mail) return;
          e.preventDefault();
          onOpen(mail.id);
          return;
        }
        case "Escape": {
          e.preventDefault();
          requestSelectMail(null);
          clearSelection();
          return;
        }
        case "Delete": {
          e.preventDefault();
          act("trash");
          return;
        }
      }

      const action = matchAction(e, bindings, "mail");
      switch (action) {
        case "next_message": {
          e.preventDefault();
          move(1);
          break;
        }
        case "previous_message": {
          e.preventDefault();
          move(-1);
          break;
        }
        case "toggle_select": {
          const mail = getCurrentMail();
          if (!mail) return;
          e.preventDefault();
          toggleSelection(mail);
          break;
        }
        case "archive": {
          e.preventDefault();
          act("archive");
          break;
        }
        case "trash": {
          e.preventDefault();
          act("trash");
          break;
        }
        case "spam": {
          e.preventDefault();
          act("spam");
          break;
        }
        case "toggle_read": {
          e.preventDefault();
          // No single message to read a toggle target from once several
          // are ticked -- "mark read" is what the bulk panel itself offers
          // a button for, "mark unread" stays reachable through its own
          // shortcut.
          if (multiSelect) {
            act("mark_read");
            break;
          }
          const mail = getCurrentMail();
          if (mail) act(isRowUnread(mail) ? "mark_read" : "mark_unread");
          break;
        }
        case "mark_unread": {
          e.preventDefault();
          act("mark_unread");
          break;
        }
        case "toggle_star": {
          e.preventDefault();
          // Same reasoning as toggle_read -- and the bulk panel itself
          // offers only "Star", never an "Unstar", over a selection.
          if (multiSelect) {
            act("flag");
            break;
          }
          const mail = getCurrentMail();
          if (mail) act(mail.is_flagged ? "unflag" : "flag");
          break;
        }
        case "compose": {
          e.preventDefault();
          setComposeIntent({});
          break;
        }
        case "reply":
        case "reply_all":
        case "forward": {
          // Acts on the open reading pane, not merely a focused row --
          // openIndex, not currentIndex, is "something is actually open".
          // A multi-selection replaces that pane with the bulk panel, so
          // there is nothing here to reply to even when openIndex still
          // names a message.
          if (openIndex < 0 || multiSelect) return;
          e.preventDefault();
          const mode = action === "reply_all" ? "reply-all" : action === "forward" ? "forward" : "reply";
          setRequestReplyMode({ mode, nonce: Date.now() });
          break;
        }
        case "move": {
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
    bindings,
  ]);
}
