/**
 * Keyboard shortcuts for mail navigation and actions.
 *
 * Web-only; disabled when an input/textarea is focused.
 */

"use client";

import { useEffect, useCallback } from "react";
import { useAtom, useAtomValue, useSetAtom } from "jotai";
import { focusedMailIndexAtom } from "@/store/focused-mail-atom";
import { selectedMailIdAtom, selectedAccountIdAtom } from "@/lib/atoms";
import { useMailAction } from "@/hooks/use-mails";
import { useToggleSelection } from "@/hooks/use-selection";
import type { MessageSummary } from "@/types/api";

/** Whether an element is an interactive input that should suppress shortcuts. */
function isEditableElement(el: EventTarget | null): boolean {
  if (!el || !(el instanceof HTMLElement)) return false;
  const tag = el.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    el.isContentEditable
  );
}

interface UseKeyboardShortcutsOptions {
  /** Current visible mail list. */
  mails: MessageSummary[];
  /** Callback to scroll the VList to a given index. */
  scrollToIndex?: (index: number) => void;
}

/**
 * Registers global keyboard shortcuts for mail navigation and actions.
 *
 * Shortcuts:
 * - j/k: navigate down/up in mail list
 * - Enter: open focused mail in reading pane
 * - Escape: close reading pane / clear focus
 * - x: toggle selection on focused mail
 * - e: archive focused mail
 * - #: delete focused mail
 * - !: mark focused mail as spam
 * - r: mark focused mail as read
 * - u: mark focused mail as unread
 * - s: toggle star on focused mail
 */
export function useKeyboardShortcuts({
  mails,
  scrollToIndex,
}: UseKeyboardShortcutsOptions) {
  const [focusedIndex, setFocusedIndex] = useAtom(focusedMailIndexAtom);
  const setSelectedMailId = useSetAtom(selectedMailIdAtom);
  const accountId = useAtomValue(selectedAccountIdAtom);
  const mailAction = useMailAction();
  const toggleSelection = useToggleSelection();

  const getFocusedMail = useCallback((): MessageSummary | null => {
    if (focusedIndex < 0 || focusedIndex >= mails.length) return null;
    return mails[focusedIndex];
  }, [focusedIndex, mails]);

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (isEditableElement(e.target)) return;

      switch (e.key) {
        case "j": {
          e.preventDefault();
          const next = Math.min(focusedIndex + 1, mails.length - 1);
          setFocusedIndex(next);
          scrollToIndex?.(next);
          break;
        }
        case "k": {
          e.preventDefault();
          const prev = Math.max(focusedIndex - 1, 0);
          setFocusedIndex(prev);
          scrollToIndex?.(prev);
          break;
        }
        case "Enter": {
          e.preventDefault();
          const mail = getFocusedMail();
          if (mail) setSelectedMailId(mail.id);
          break;
        }
        case "Escape": {
          e.preventDefault();
          setSelectedMailId(null);
          break;
        }
        case "x": {
          e.preventDefault();
          const mail = getFocusedMail();
          if (mail && accountId) {
            toggleSelection.mutate({ accountId, body: { message_id: mail.id } });
          }
          break;
        }
        case "e": {
          e.preventDefault();
          const mail = getFocusedMail();
          if (mail && accountId) {
            mailAction.mutate({
              mailId: mail.id,
              accountId,
              action: { action: "archive" },
            });
          }
          break;
        }
        case "#": {
          e.preventDefault();
          const mail = getFocusedMail();
          if (mail && accountId) {
            mailAction.mutate({
              mailId: mail.id,
              accountId,
              action: { action: "delete" },
            });
          }
          break;
        }
        case "!": {
          e.preventDefault();
          const mail = getFocusedMail();
          if (mail && accountId) {
            mailAction.mutate({
              mailId: mail.id,
              accountId,
              action: { action: "spam" },
            });
          }
          break;
        }
        case "r": {
          e.preventDefault();
          const mail = getFocusedMail();
          if (mail && accountId) {
            mailAction.mutate({
              mailId: mail.id,
              accountId,
              action: { action: "mark_read" },
            });
          }
          break;
        }
        case "u": {
          e.preventDefault();
          const mail = getFocusedMail();
          if (mail && accountId) {
            mailAction.mutate({
              mailId: mail.id,
              accountId,
              action: { action: "mark_unread" },
            });
          }
          break;
        }
        case "s": {
          e.preventDefault();
          const mail = getFocusedMail();
          if (mail && accountId) {
            mailAction.mutate({
              mailId: mail.id,
              accountId,
              action: { action: mail.is_flagged ? "unflag" : "flag" },
            });
          }
          break;
        }
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [
    focusedIndex,
    mails,
    accountId,
    setFocusedIndex,
    setSelectedMailId,
    getFocusedMail,
    mailAction,
    toggleSelection,
    scrollToIndex,
  ]);
}
