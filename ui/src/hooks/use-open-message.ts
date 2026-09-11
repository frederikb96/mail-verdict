"use client";

/**
 * Opening a message from outside the list it lives in -- an alert in the
 * bell, a system notification, a search hit, a `?message=` link. One place
 * decides where it lands, so every entry point agrees:
 *
 * - where the message is now, never where it was when the link was made:
 *   its location is asked for fresh, and GET /messages/{id}/location
 *   follows a move made in another mail client too;
 * - inside the most recently opened unified view containing its folder,
 *   when a unified view is where the reader last was;
 * - otherwise in its own account's folder.
 *
 * The list there centres its first page on the message
 * (pendingAroundMailIdAtom), unless it is the list already on screen and
 * already holds the message -- re-centring that would only rebuild it.
 */

import { useCallback, useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useQueryClient, type InfiniteData, type QueryClient } from "@tanstack/react-query";
import { useAtomValue, useSetAtom, useStore } from "jotai";

import { api } from "@/lib/api";
import { buildMailUrl } from "@/lib/mail-url";
import { unifiedKeys } from "@/hooks/use-unified-view";
import { useToast } from "@/hooks/use-toast";
import {
  isUnifiedViewAtom,
  lastMailViewWasUnifiedAtom,
  pendingAroundMailIdAtom,
  recentUnifiedViewsAtom,
  requestSelectMailAtom,
  selectedAccountIdAtom,
  selectedFolderIdAtom,
  selectedUnifiedFolderAtom,
} from "@/lib/atoms";
import type { MessageListResponse, MessageLocation, UnifiedFolderResponse } from "@/types/api";

const RECENT_UNIFIED_VIEWS = 5;

export interface MessagePlace {
  location: MessageLocation;
  /** The unified view to open it in, by name; null for its own folder. */
  unifiedView: string | null;
}

/** Whether a list for this place already holds the message -- by id, or in
 * a threaded list by its conversation, whose row may be a newer message of
 * the same thread. */
function isLoaded(qc: QueryClient, prefix: string[], location: MessageLocation): boolean {
  for (const query of qc.getQueryCache().findAll({ queryKey: prefix })) {
    const threaded = query.queryKey.includes("threaded");
    const data = query.state.data as InfiniteData<MessageListResponse> | undefined;
    const found = data?.pages?.some((page) =>
      page.messages.some(
        (m) => m.id === location.id || (threaded && m.thread_id === location.thread_id),
      ),
    );
    if (found) return true;
  }
  return false;
}

export function useOpenMessage() {
  const qc = useQueryClient();
  const store = useStore();
  const router = useRouter();
  const pathname = usePathname();
  const { push: toast } = useToast();
  const setAccountId = useSetAtom(selectedAccountIdAtom);
  const setFolderId = useSetAtom(selectedFolderIdAtom);
  const setUnifiedFolder = useSetAtom(selectedUnifiedFolderAtom);
  const setLastUnified = useSetAtom(lastMailViewWasUnifiedAtom);
  const requestSelectMail = useSetAtom(requestSelectMailAtom);
  const setPendingAround = useSetAtom(pendingAroundMailIdAtom);

  const resolve = useCallback(
    async (location: MessageLocation): Promise<MessagePlace> => {
      const recent = store.get(recentUnifiedViewsAtom);
      if (!store.get(lastMailViewWasUnifiedAtom) || recent.length === 0) {
        return { location, unifiedView: null };
      }
      const views = await qc.fetchQuery<UnifiedFolderResponse[]>({
        queryKey: unifiedKeys.folders,
        queryFn: () => api.unified.folders(),
        staleTime: 30_000,
      });
      const unifiedView =
        recent.find((name) =>
          views
            .find((v) => v.unified_name === name)
            ?.folders.some((f) => f.folder_id === location.folder_id),
        ) ?? null;
      return { location, unifiedView };
    },
    [qc, store],
  );

  const resolveById = useCallback(
    async (messageId: string) => resolve(await api.mails.location(messageId)),
    [resolve],
  );

  const apply = useCallback(
    ({ location, unifiedView }: MessagePlace) => {
      const alreadyHere = unifiedView
        ? store.get(isUnifiedViewAtom) && store.get(selectedUnifiedFolderAtom) === unifiedView
        : store.get(selectedAccountIdAtom) === location.account_id &&
          store.get(selectedFolderIdAtom) === location.folder_id;
      const prefix = unifiedView
        ? ["unified", "mails", unifiedView]
        : ["mails", location.account_id, location.folder_id];
      const centre = !(alreadyHere && isLoaded(qc, prefix, location));

      if (unifiedView) {
        setAccountId("unified");
        setFolderId(null);
        setUnifiedFolder(unifiedView);
      } else {
        setAccountId(location.account_id);
        setFolderId(location.folder_id);
        setLastUnified(false);
      }
      requestSelectMail(location.id);
      if (centre) setPendingAround({ id: location.id, threadId: location.thread_id });

      if (pathname !== "/") {
        router.push(
          buildMailUrl({
            accountId: unifiedView ? "unified" : location.account_id,
            isUnified: unifiedView !== null,
            unifiedFolder: unifiedView,
            folderId: unifiedView ? null : location.folder_id,
            messageId: location.id,
          }),
        );
      }
    },
    [
      qc, store, pathname, router, setAccountId, setFolderId, setUnifiedFolder, setLastUnified,
      requestSelectMail, setPendingAround,
    ],
  );

  const openMessageById = useCallback(
    async (messageId: string): Promise<boolean> => {
      try {
        apply(await resolveById(messageId));
        return true;
      } catch {
        toast("That message no longer exists", "error");
        return false;
      }
    },
    [apply, resolveById, toast],
  );

  return { resolveById, apply, openMessageById };
}

/**
 * Records a unified view as the reader's latest one, whenever one is on
 * screen. Only this side is recorded from state: leaving for an account's
 * own folder is recorded where the reader chooses it (useMarkAccountView),
 * because an account is also selected automatically on load -- recorded
 * from state, that would erase "last in a unified view" before a
 * notification opening a fresh window had a chance to read it.
 */
export function useRecordUnifiedView(): void {
  const accountId = useAtomValue(selectedAccountIdAtom);
  const unifiedFolder = useAtomValue(selectedUnifiedFolderAtom);
  const setRecent = useSetAtom(recentUnifiedViewsAtom);
  const setLastUnified = useSetAtom(lastMailViewWasUnifiedAtom);

  useEffect(() => {
    if (accountId !== "unified" || !unifiedFolder) return;
    setLastUnified(true);
    setRecent((previous) =>
      previous[0] === unifiedFolder
        ? previous
        : [unifiedFolder, ...previous.filter((name) => name !== unifiedFolder)].slice(
            0, RECENT_UNIFIED_VIEWS,
          ),
    );
  }, [accountId, unifiedFolder, setRecent, setLastUnified]);
}

/** For a control the reader uses to pick an account's own folder. */
export function useMarkAccountView(): () => void {
  const setLastUnified = useSetAtom(lastMailViewWasUnifiedAtom);
  return useCallback(() => setLastUnified(false), [setLastUnified]);
}
