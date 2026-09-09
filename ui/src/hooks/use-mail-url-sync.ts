"use client";

/**
 * The mail view's own URL, `/?account=&folder=&message=` -- read on a
 * cold load or a back/forward navigation, written whenever the selection
 * changes. This hook is the ONLY thing that calls router.push/replace for
 * this route, the same single-writer discipline
 * use-calendar-navigate.ts/use-calendar-url-sync.ts already established:
 * a second writer racing this one is what silently downgrades a push to
 * a replace and leaves the back button with nothing to return to.
 *
 * Unlike the calendar's pair of hooks, this is one hook rather than two --
 * mail selection is already set from many places (the sidebar, a row
 * click, the reading pane's own navigation), all through the ordinary
 * selection atoms, and asking every one of those call sites to route
 * through a shared navigate() function is a much larger change than the
 * URL itself needs. Watching the atoms and writing the URL to match them
 * keeps those call sites untouched while still being exactly one writer.
 *
 * push vs replace: a message opening (transitioning to a new, different,
 * non-null id) gets its own history entry, so pressing Back after opening
 * several messages in turn lands on each in reverse order -- section D's
 * own acceptance test. Every other change (switching folder or account,
 * or a message closing back to none) replaces, the same way the folder
 * sidebar's own clicks always have -- otherwise browsing folders would
 * flood history with an entry per folder switch.
 *
 * Two things the write effect below does to keep an account or folder
 * switch to exactly one navigation, deferred past the click that made
 * it: skipping the transient shape where a just-picked account (or the
 * unified view) has no folder resolved yet -- app-sidebar.tsx's own
 * auto-select effects fill that in moments later, and writing for it
 * anyway means two navigations for one click instead of one -- and
 * firing the push/replace call itself only once the browser has painted
 * and drained the resulting task, not in the same commit as the atom
 * change. Both exist because account and folder selection are set from
 * a dropdown or a sheet closing itself as part of the same click, and a
 * navigation landing in the middle of that close can detach the element
 * the click landed on before the browser is done with it.
 */

import { useEffect, useRef } from "react";
import { useAtom, useAtomValue, useSetAtom } from "jotai";
import { useRouter, useSearchParams } from "next/navigation";
import {
  pendingAroundMailIdAtom,
  requestSelectMailAtom,
  selectedAccountIdAtom,
  selectedFolderIdAtom,
  selectedMailIdAtom,
  selectedUnifiedFolderAtom,
} from "@/lib/atoms";
import { buildMailUrl } from "@/lib/mail-url";
import { api } from "@/lib/api";

export function useMailUrlSync(): void {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [accountId, setAccountId] = useAtom(selectedAccountIdAtom);
  const [folderId, setFolderId] = useAtom(selectedFolderIdAtom);
  const [unifiedFolder, setUnifiedFolder] = useAtom(selectedUnifiedFolderAtom);
  const messageId = useAtomValue(selectedMailIdAtom);
  // Opening a message via the URL respects an in-progress dirty reply the
  // same way every other message-open action does -- requestSelectMailAtom,
  // not the bare atom, matches search-page.tsx's own openResult.
  const requestSelectMail = useSetAtom(requestSelectMailAtom);
  const setPendingAroundMailId = useSetAtom(pendingAroundMailIdAtom);
  const isUnified = accountId === "unified";

  // What the URL last named, for the cold-read effect below to compare
  // against -- resolving a message id needs an async fetch, so this
  // guards against re-fetching (and re-applying) the same `?message=`
  // on every unrelated render.
  const lastReadParamsRef = useRef<string | null>(null);

  // Set for the duration of a cold read's async resolution -- the write
  // effect below must not run while one is in flight, or it computes its
  // target URL from atoms that are still at their pre-read values (the
  // fetch hasn't resolved yet) and overwrites the very URL being read,
  // wiping `?message=` before its fetch ever completes.
  const applyingUrlRef = useRef(false);

  // Cold read: on mount, and again whenever the URL changes from outside
  // this hook's own write effect below (a paste, a back/forward nav).
  //
  // The `?message=` branch is the only asynchronous one (it needs a
  // fetch to resolve account/folder) -- applyingUrlRef guards only that
  // branch, set before the fetch starts and cleared once it resolves.
  // The other branches update the atoms synchronously, in the same tick
  // this effect runs in, with nothing for the write effect below to race:
  // by the time it runs (same commit, declared after this one), the
  // atoms already hold the values just read from the URL.
  useEffect(() => {
    const raw = searchParams.toString();
    if (raw === lastReadParamsRef.current) return;
    lastReadParamsRef.current = raw;

    const paramAccount = searchParams.get("account");
    const paramFolder = searchParams.get("folder");
    const paramMessage = searchParams.get("message");

    // `?message=` alone is a complete link -- GET /api/messages/{id}
    // returns account_id/folder_id, so the account/folder params (if any)
    // are redundant with it rather than needed alongside it.
    if (paramMessage && paramMessage !== messageId) {
      let cancelled = false;
      applyingUrlRef.current = true;
      (async () => {
        try {
          const mail = await api.mails.get(paramMessage);
          if (cancelled) return;
          setAccountId(mail.account_id);
          setFolderId(mail.folder_id);
          requestSelectMail(mail.id);
          // The list this lands in may not have this message on its
          // newest page -- centre the very first page on it, the same
          // reveal step a search result opened from search-page.tsx uses.
          setPendingAroundMailId({ id: mail.id, threadId: mail.thread_id });
        } catch {
          // A moved-or-resynced message id: leave the current selection
          // alone rather than clearing it out from under the reader over
          // a dead link nobody asked to follow.
        } finally {
          if (!cancelled) applyingUrlRef.current = false;
        }
      })();
      return () => {
        cancelled = true;
      };
    }

    if (paramAccount === "unified") {
      if (accountId !== "unified") setAccountId("unified");
      if (paramFolder && paramFolder !== unifiedFolder) setUnifiedFolder(paramFolder);
    } else if (paramAccount && paramAccount !== accountId) {
      setAccountId(paramAccount);
      if (paramFolder) setFolderId(paramFolder);
    } else if (paramAccount && paramFolder && paramFolder !== folderId) {
      setFolderId(paramFolder);
    }
    return undefined;
    // Deliberately reacting to the URL only -- the atoms it reads for
    // comparison are read fresh via the closure, not tracked as deps,
    // since this must run only when the URL itself moved.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  // Write: the URL always follows the current selection. previousMessageIdRef
  // starts at whatever the selection already was (not undefined), so the
  // very first run -- including one that just applied a cold read above --
  // finds no change and replaces rather than pushing a redundant entry.
  const previousMessageIdRef = useRef(messageId);

  useEffect(() => {
    // A cold read's message fetch is still in flight -- the atoms below
    // do not yet reflect it, and writing now would overwrite the very
    // `?message=` this render is in the middle of resolving.
    if (applyingUrlRef.current) return;

    // An account (or the unified view) with no folder resolved yet is
    // always transient -- app-sidebar.tsx's own auto-select effects pick
    // one moments later, once that account's folders have loaded. Without
    // this, a switch writes the URL twice: once with no folder the
    // instant the account changes, and again once the folder resolves --
    // two navigations for one click is twice the chance of the dropdown's
    // own close landing inside one of them.
    if ((!isUnified && accountId && !folderId) || (isUnified && !unifiedFolder)) {
      return;
    }

    const target = buildMailUrl({
      accountId, isUnified, unifiedFolder, folderId, messageId,
    });
    const current = `/${searchParams.toString() ? `?${searchParams.toString()}` : ""}`;
    const previousMessageId = previousMessageIdRef.current;
    previousMessageIdRef.current = messageId;

    if (target === current) {
      // Nothing to write, but keep lastReadParamsRef in step so the
      // cold-read effect above does not mistake this render for an
      // external URL change on its next pass.
      return;
    }

    const opensNewMessage = messageId !== null && messageId !== previousMessageId;
    lastReadParamsRef.current = target.includes("?") ? target.slice(target.indexOf("?") + 1) : "";

    // Deferred past the current paint and task: this effect fires the
    // moment the selection atoms change, which for the account switcher
    // and a folder click is the very same commit that closes the
    // control's own dropdown or sheet. Firing the navigation in that
    // same frame races it against that close -- Next's own
    // navigation-in-progress tracking can catch the click still being
    // delivered and detach the element it landed on before the browser
    // finishes with it. requestAnimationFrame alone waits for the next
    // paint; chaining a setTimeout onto it also waits for that paint's
    // own task to drain, which is what actually matters under load --
    // a starved renderer delays the close and the frame together, so a
    // bare frame does not reliably run after it. Neither is observable
    // as a delay in the URL itself.
    let timeout: ReturnType<typeof setTimeout> | undefined;
    const frame = requestAnimationFrame(() => {
      timeout = setTimeout(() => {
        if (opensNewMessage) {
          router.push(target, { scroll: false });
        } else {
          router.replace(target, { scroll: false });
        }
      }, 0);
    });
    return () => {
      cancelAnimationFrame(frame);
      if (timeout !== undefined) clearTimeout(timeout);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId, isUnified, unifiedFolder, folderId, messageId]);
}
