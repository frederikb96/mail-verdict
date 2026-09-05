"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation } from "@tanstack/react-query";
import { useAtom, useAtomValue, useSetAtom } from "jotai";
import { VList, type VListHandle } from "virtua";
import { AlertCircle, Folder as FolderIcon, Loader2, Search as SearchIcon } from "lucide-react";

import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Skeleton } from "@/components/ui/skeleton";
import { FolderPicker } from "@/components/search/folder-picker";
import { SearchResultRow } from "@/components/search/search-result-row";

import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useSearchResults } from "@/hooks/use-search";
import { useSearchFolders } from "@/hooks/use-search-folders";
import {
  ALL_SEARCH_FIELDS,
  searchFieldsAtom,
  searchFolderIdsAtom,
  searchQueryAtom,
  searchScrollAnchorAtom,
  searchScrollCacheAtom,
  searchSemanticModeAtom,
  searchStrictnessAtom,
} from "@/lib/search-prefs";
import {
  selectedAccountIdAtom,
  selectedFolderIdAtom,
  selectedMailIdAtom,
  isUnifiedViewAtom,
} from "@/lib/atoms";
import type { SearchField, SearchStrictness } from "@/types/api";

const FIELD_LABELS: Record<SearchField, string> = {
  subject: "Subject",
  from: "From",
  to: "To",
  body: "Body",
};

const STRICTNESS_LABELS: Record<SearchStrictness, string> = {
  loose: "Loose",
  balanced: "Balanced",
  strict: "Strict",
};

const LOAD_MORE_SENTINEL_KEY = "__search-load-more__";

export function SearchPage() {
  const [rawQuery, setRawQuery] = useAtom(searchQueryAtom);
  const [query, setQuery] = useState(rawQuery);
  const router = useRouter();
  const selectedAccountId = useAtomValue(selectedAccountIdAtom);
  const isUnified = useAtomValue(isUnifiedViewAtom);
  const setSelectedAccountId = useSetAtom(selectedAccountIdAtom);
  const setSelectedFolderId = useSetAtom(selectedFolderIdAtom);
  const setSelectedMailId = useSetAtom(selectedMailIdAtom);

  const [fields, setFields] = useAtom(searchFieldsAtom);
  const [folderIds, setFolderIds] = useAtom(searchFolderIdsAtom);
  const [semantic, setSemantic] = useAtom(searchSemanticModeAtom);
  const [strictness, setStrictness] = useAtom(searchStrictnessAtom);
  const [scrollAnchor, setScrollAnchor] = useAtom(searchScrollAnchorAtom);
  const [scrollCache, setScrollCache] = useAtom(searchScrollCacheAtom);

  const vlistRef = useRef<VListHandle>(null);

  // The backend is now single-digit-to-low-double-digit milliseconds
  // (the trigram-over-every-body query this replaced was the thing that
  // needed 250ms of debounce); 150ms still absorbs ordinary typing
  // without a request per keystroke.
  useEffect(() => {
    const timer = setTimeout(() => setQuery(rawQuery), 150);
    return () => clearTimeout(timer);
  }, [rawQuery]);

  const searchAccountId = isUnified ? undefined : (selectedAccountId ?? undefined);

  // An explicitly-cleared folder scope ([] -- see search-prefs.ts) means
  // "search nothing", distinct from null ("every folder"). useSearchResults
  // itself refuses to run the query in this state; this is only what the
  // page shows instead of "No results found", which would read as a
  // real, contentful answer rather than as nothing having been asked yet.
  const hasFolderScope = folderIds === null || folderIds.length > 0;

  const { data, isLoading, isFetchingNextPage, hasNextPage, fetchNextPage, isError, error } =
    useSearchResults({
      query,
      accountId: searchAccountId,
      folderIds,
      fields,
      semantic,
      strictness,
    });

  // The picker's own options, scoped the same way the search itself is --
  // an explicit folder selection made under a different account can name
  // no folder visible here, which the server ANDs into a query that can
  // never match anything, presented as an ordinary "No results found"
  // rather than the account-mismatch it actually is.
  const { options: scopedFolderOptions, isLoading: scopedFoldersLoading } =
    useSearchFolders(searchAccountId);
  useEffect(() => {
    // An explicit, deliberately-empty selection is left alone here -- it
    // isn't "drifted outside what's visible under this account", it's a
    // real state the reader chose, and this effect exists to catch the
    // former, not silently undo the latter.
    if (folderIds === null || folderIds.length === 0 || scopedFoldersLoading) return;
    const visibleIds = new Set(scopedFolderOptions.map((o) => o.folder.id));
    if (!folderIds.some((id) => visibleIds.has(id))) {
      // Falls back to "every folder in this scope" -- the same state the
      // picker already collapses to once every visible folder is ticked.
      setFolderIds(null);
    }
    // Re-evaluate only when the account scope (or its folder list
    // resolving) changes, not on every unrelated folder-list refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchAccountId, scopedFoldersLoading]);

  const results = useMemo(() => data?.pages.flatMap((p) => p.items) ?? [], [data]);
  const total = data?.pages[0]?.total ?? 0;

  // A search result carries no account/folder context for opening -- it's
  // resolved from the message itself, then handed to the mail view's own
  // selection atoms exactly like the old search page did.
  const openResult = useMutation({
    mutationFn: (messageId: string) => api.mails.get(messageId),
    onSuccess: (mail) => {
      setSelectedAccountId(mail.account_id);
      setSelectedFolderId(mail.folder_id);
      setSelectedMailId(mail.id);
      router.push("/");
    },
  });

  // One string identifying exactly this search -- keys the VList (so a
  // genuinely new query starts at the top rather than inheriting the
  // previous query's scroll offset, clamped) and ties a persisted scroll
  // anchor/cache to the search it was captured under, so a return to a
  // DIFFERENT search can never apply a stale one left over from another.
  const listIdentity = useMemo(
    () =>
      [
        semantic ? "semantic" : "fulltext",
        query,
        searchAccountId ?? "all",
        (folderIds ?? []).slice().sort().join(","),
        semantic ? strictness : [...fields].sort().join(","),
      ].join("|"),
    [semantic, query, searchAccountId, folderIds, strictness, fields],
  );

  // --- Scroll position restore-and-hold (SKILL.md) ---
  //
  // Anchored on the row's own identity, never a pixel offset. Restoring
  // is a two-part job: scrollToIndex once the anchor's page has loaded,
  // then hold that position while later rows mount and measure under it
  // -- a row settling in above the anchor moves it, which a hold
  // re-asserts against every render until something releases it.
  const restorableAnchorId =
    scrollAnchor?.listIdentity === listIdentity ? scrollAnchor.messageId : null;
  const restorableCache =
    scrollCache?.listIdentity === listIdentity ? scrollCache.cache : undefined;

  const holdingRef = useRef(false);
  const restoreAttemptedRef = useRef(false);
  // The offset we ourselves last wrote via scrollToIndex, so the next
  // onScroll event it produces can be told apart from the reader's own
  // gesture -- the latter is what releases the hold.
  const ownWriteOffsetRef = useRef<number | null>(null);

  useEffect(() => {
    // A new list identity is a fresh attempt -- including one that
    // failed to find its anchor and gave up (see below), which must not
    // keep retrying against results that have since scrolled past it.
    restoreAttemptedRef.current = false;
    holdingRef.current = false;
    ownWriteOffsetRef.current = null;
  }, [listIdentity]);

  const writeScrollToIndex = useCallback((index: number) => {
    const handle = vlistRef.current;
    if (!handle) return;
    handle.scrollToIndex(index, { align: "start" });
    ownWriteOffsetRef.current = handle.scrollOffset;
  }, []);

  useEffect(() => {
    if (restoreAttemptedRef.current) return;
    if (!restorableAnchorId || isLoading || results.length === 0) return;
    const index = results.findIndex((r) => r.id === restorableAnchorId);
    if (index === -1) {
      // Not on the page(s) loaded so far. Semantic mode never has a
      // further page to fetch, and fulltext gives up to the top rather
      // than growing an unbounded fetch just to find a row that may no
      // longer even match -- see the Friction note on a bounded
      // widen-and-retry for a later pass.
      restoreAttemptedRef.current = true;
      return;
    }
    restoreAttemptedRef.current = true;
    holdingRef.current = true;
    writeScrollToIndex(index);
  }, [restorableAnchorId, results, isLoading, writeScrollToIndex]);

  // Re-assert every render while holding -- rows mounting and measuring
  // below the anchor do not move it (below the viewport), but virtua's
  // own cache warming as later rows are measured can still nudge the
  // scroll offset; this keeps correcting it back until release.
  useLayoutEffect(() => {
    if (!holdingRef.current || !restorableAnchorId) return;
    const index = results.findIndex((r) => r.id === restorableAnchorId);
    if (index !== -1) writeScrollToIndex(index);
  });

  const persistAnchor = useCallback(
    (offset: number) => {
      const handle = vlistRef.current;
      if (!handle) return;
      const index = handle.findItemIndex(offset);
      const row = results[index];
      if (!row) return;
      setScrollAnchor((prev) =>
        prev?.messageId === row.id && prev.listIdentity === listIdentity
          ? prev
          : { listIdentity, messageId: row.id },
      );
      setScrollCache({ listIdentity, cache: handle.cache });
    },
    [results, listIdentity, setScrollAnchor, setScrollCache],
  );

  const handleScroll = useCallback(
    (offset: number) => {
      if (holdingRef.current) {
        const expected = ownWriteOffsetRef.current;
        const isOwnWrite = expected !== null && Math.abs(offset - expected) <= 1;
        if (!isOwnWrite) {
          // A deliberate gesture from the reader -- release the hold
          // rather than fighting it on the very next render.
          holdingRef.current = false;
        } else {
          return; // still settling from our own write; nothing else to do yet
        }
      }

      if (!vlistRef.current) return;
      persistAnchor(offset);

      // A viewport or two of margin, not the very edge -- the load
      // happens off-screen rather than the reader watching it.
      const { scrollSize, viewportSize } = vlistRef.current;
      if (
        scrollSize - offset - viewportSize < viewportSize * 1.5 &&
        hasNextPage &&
        !isFetchingNextPage
      ) {
        fetchNextPage();
      }
    },
    [hasNextPage, isFetchingNextPage, fetchNextPage, persistAnchor],
  );

  const toggleField = (field: SearchField) => {
    setFields((prev) => {
      if (prev.includes(field)) {
        // At least one field must stay selected -- otherwise every
        // search matches nothing, with no visible reason why.
        if (prev.length === 1) return prev;
        return prev.filter((f) => f !== field);
      }
      return [...prev, field];
    });
  };

  const showEmptyPrompt = query.trim().length < 2 && hasFolderScope;
  const showNoFolderSelected = !showEmptyPrompt && !hasFolderScope;
  // A failed query (a semantic search with no provider configured, most
  // commonly) reads as "No results found" otherwise -- a lie the user
  // will act on by rephrasing or giving up on the feature.
  const showError = !showEmptyPrompt && !showNoFolderSelected && isError;
  const showNoResults =
    !showEmptyPrompt && !showNoFolderSelected && !isLoading && !showError && results.length === 0;
  const errorMessage =
    semantic && error instanceof ApiError && error.status === 503
      ? "Semantic search is unavailable -- no AI provider is configured for it."
      : error?.message || "Search failed.";

  return (
    <div className="flex h-full flex-col gap-3 p-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">Search</h1>
        {!showEmptyPrompt && !showNoFolderSelected && !isLoading && !showError && (
          <span className="text-xs text-muted-foreground" data-testid="search-result-count">
            {total} {total === 1 ? "result" : "results"}
          </span>
        )}
      </div>

      <div className="relative">
        <SearchIcon className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={rawQuery}
          onChange={(e) => setRawQuery(e.target.value)}
          placeholder="Search messages…"
          className="pl-9"
        />
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <FolderPicker selectedIds={folderIds} onChange={setFolderIds} accountId={searchAccountId} />

        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          <Switch checked={semantic} onCheckedChange={setSemantic} />
          Semantic search
        </label>

        {!semantic && (
          <div className="flex flex-wrap gap-1">
            {ALL_SEARCH_FIELDS.map((field) => (
              <button
                key={field}
                type="button"
                onClick={() => toggleField(field)}
                className={cn(
                  "rounded-full border px-2 py-0.5 text-xs",
                  fields.includes(field) ? "border-primary bg-primary/10" : "text-muted-foreground",
                )}
              >
                {FIELD_LABELS[field]}
              </button>
            ))}
          </div>
        )}

        {semantic && (
          <div className="flex flex-wrap gap-1" data-testid="search-strictness">
            {(Object.keys(STRICTNESS_LABELS) as SearchStrictness[]).map((level) => (
              <button
                key={level}
                type="button"
                onClick={() => setStrictness(level)}
                className={cn(
                  "rounded-full border px-2 py-0.5 text-xs",
                  strictness === level ? "border-primary bg-primary/10" : "text-muted-foreground",
                )}
              >
                {STRICTNESS_LABELS[level]}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="flex-1 overflow-hidden rounded-lg border">
        {isLoading && (
          <div className="flex flex-col gap-3 p-4">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-16 w-full" />
            ))}
          </div>
        )}

        {showEmptyPrompt && !isLoading && (
          <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
            <SearchIcon className="h-12 w-12 opacity-50" />
            <p>Enter at least 2 characters to search</p>
          </div>
        )}

        {showNoFolderSelected && (
          <div
            className="flex h-full flex-col items-center justify-center gap-3 p-8 text-muted-foreground"
            data-testid="search-no-folder-selected"
          >
            <FolderIcon className="h-12 w-12 opacity-50" />
            <p>Select at least one folder to search</p>
          </div>
        )}

        {showError && (
          <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
            <AlertCircle className="h-12 w-12 opacity-50" />
            <p>{errorMessage}</p>
          </div>
        )}

        {showNoResults && (
          <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
            <SearchIcon className="h-12 w-12 opacity-50" />
            <p>No results found</p>
          </div>
        )}

        {!isLoading && results.length > 0 && (
          <VList
            key={listIdentity}
            id="search-results-list"
            ref={vlistRef}
            className="h-full"
            itemSize={72}
            cache={restorableCache}
            onScroll={handleScroll}
          >
            {[
              ...results.map((result) => (
                <SearchResultRow key={result.id} result={result} onOpen={openResult.mutate} />
              )),
              // A sentinel row inside the list, not a sibling below it --
              // "I scroll further down and see just a spinner there" is
              // what a load-more indicator sitting inside the scrollable
              // area, not clipped below the viewport, actually means.
              isFetchingNextPage ? (
                <div
                  key={LOAD_MORE_SENTINEL_KEY}
                  className="flex items-center justify-center py-3"
                  data-testid="search-load-more-spinner"
                >
                  <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                </div>
              ) : null,
            ].filter((el): el is NonNullable<typeof el> => el !== null)}
          </VList>
        )}
      </div>
    </div>
  );
}
