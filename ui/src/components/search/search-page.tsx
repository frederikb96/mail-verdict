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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { FolderPicker } from "@/components/search/folder-picker";
import { TimePicker, TimeWindowChip } from "@/components/search/time-picker";
import { SearchResultRow } from "@/components/search/search-result-row";

import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useSearchResults, useSemanticResults } from "@/hooks/use-search";
import { useSearchFolders } from "@/hooks/use-search-folders";
import { useAccounts } from "@/hooks/use-accounts";
import {
  ALL_SEARCH_FIELDS,
  searchAccountIdAtom,
  searchFieldsAtom,
  searchFolderIdsAtom,
  searchQueryAtom,
  searchScrollAnchorAtom,
  searchScrollCacheAtom,
  searchSemanticModeAtom,
  searchWindowAtom,
} from "@/lib/search-prefs";
import { chooseSearchView, type Fetched } from "@/lib/search-view";
import { windowKey } from "@/lib/search-window";
import { useOpenMessage } from "@/hooks/use-open-message";
import type { SearchResultItem } from "@/hooks/use-search";
import type { SearchField } from "@/types/api";

const ALL_ACCOUNTS_VALUE = "__all__";

const FIELD_LABELS: Record<SearchField, string> = {
  subject: "Subject",
  from: "From",
  to: "To",
  body: "Body",
};

const LOAD_MORE_SENTINEL_KEY = "__search-load-more__";
const LESS_LIKELY_KEY = "__search-less-likely__";

interface SearchPageProps {
  /** From the global header's search field (`?q=` on navigating here) --
   * applied once, on mount, over whatever the atom already held. Typing
   * afterward is the reader's own, and must not be overwritten by it
   * again if the atom happens to change for an unrelated reason. */
  initialQuery?: string | null;
}

export function SearchPage({ initialQuery }: SearchPageProps = {}) {
  const [rawQuery, setRawQuery] = useAtom(searchQueryAtom);
  const [query, setQuery] = useState(rawQuery);
  const router = useRouter();

  useEffect(() => {
    if (initialQuery) {
      setRawQuery(initialQuery);
      setQuery(initialQuery);
    }
    // Once, on mount -- see the prop's own doc comment.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const [fields, setFields] = useAtom(searchFieldsAtom);
  const [folderIds, setFolderIds] = useAtom(searchFolderIdsAtom);
  const [semantic, setSemantic] = useAtom(searchSemanticModeAtom);
  const [timeWindow, setTimeWindow] = useAtom(searchWindowAtom);
  const [scrollAnchor, setScrollAnchor] = useAtom(searchScrollAnchorAtom);
  const [scrollCache, setScrollCache] = useAtom(searchScrollCacheAtom);
  // The search page's own account scope -- deliberately independent of
  // the sidebar's selected account/Unified View, so searching everywhere
  // never requires switching the sidebar out of whatever account is
  // being read. null means every account.
  const [searchScopeAccountId, setSearchScopeAccountId] = useAtom(searchAccountIdAtom);
  const { data: accounts } = useAccounts();

  const vlistRef = useRef<VListHandle>(null);

  // Typing is absorbed for 300 ms before a search is sent: a semantic search
  // embeds each distinct text, and every search field in the apps waits the
  // same.
  useEffect(() => {
    const timer = setTimeout(() => setQuery(rawQuery), 300);
    return () => clearTimeout(timer);
  }, [rawQuery]);

  // A scope naming an account that no longer exists (deleted since it was
  // last picked) falls back to "every account" rather than silently
  // matching nothing.
  useEffect(() => {
    if (!accounts || searchScopeAccountId === null) return;
    if (!accounts.some((a) => a.id === searchScopeAccountId)) setSearchScopeAccountId(null);
  }, [accounts, searchScopeAccountId, setSearchScopeAccountId]);

  const searchAccountId = searchScopeAccountId ?? undefined;

  // An explicitly-cleared folder scope ([] -- see search-prefs.ts) means
  // "search nothing", distinct from null ("every folder"). The search hooks
  // themselves refuse to run the query in this state; this is only what the
  // page shows instead of "No results found", which would read as a
  // real, contentful answer rather than as nothing having been asked yet.
  const hasFolderScope = folderIds === null || folderIds.length > 0;

  // In semantic mode both searches run for the same text: the text one
  // answers in milliseconds and is shown at once, the one by meaning replaces
  // it when it lands (see lib/search-view.ts).
  const text = useSearchResults({
    query,
    accountId: searchAccountId,
    folderIds,
    fields,
    window: timeWindow,
  });
  const meaning = useSemanticResults({
    query,
    accountId: searchAccountId,
    folderIds,
    window: timeWindow,
    enabled: semantic,
  });
  const { isFetchingNextPage, hasNextPage, fetchNextPage } = text;
  const queryKey = [
    query.trim(), searchAccountId ?? "all", (folderIds ?? []).slice().sort().join(","),
    windowKey(timeWindow),
  ].join("|");
  const textItems = useMemo(() => text.data?.pages.flatMap((p) => p.items) ?? [], [text.data]);
  const view = chooseSearchView({
    semantic,
    currentKey: queryKey,
    text: {
      key: queryKey,
      status: text.isError ? "error" : text.data ? "success" : "pending",
      items: textItems,
      errorMessage: text.error?.message,
    } satisfies Fetched<SearchResultItem>,
    meaning: {
      key: queryKey,
      status: meaning.isError ? "error" : meaning.data ? "success" : "pending",
      items: meaning.data,
      errorStatus: meaning.error instanceof ApiError ? meaning.error.status : undefined,
      errorMessage: meaning.error?.message,
    } satisfies Fetched<SearchResultItem>,
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

  const showEmptyPrompt = query.trim().length < 2 && hasFolderScope;
  const showNoFolderSelected = !showEmptyPrompt && !hasFolderScope;
  const isLoading = view.loading && !showEmptyPrompt && !showNoFolderSelected;
  const results = view.items;
  const total = view.source === "text" ? (text.data?.pages[0]?.total ?? 0) : results.length;
  // Semantic results arrive tier 1 first; the first tier-2 row is preceded
  // by one "Less likely" divider row in the list. Row indices below are
  // list positions (divider included), result indices are positions in
  // `results`.
  const dividerResultIndex = useMemo(() => results.findIndex((r) => r.tier === 2), [results]);
  const listIndexOf = useCallback(
    (resultIndex: number) =>
      dividerResultIndex !== -1 && resultIndex >= dividerResultIndex ? resultIndex + 1 : resultIndex,
    [dividerResultIndex],
  );
  const resultIndexOf = useCallback(
    (listIndex: number) =>
      dividerResultIndex !== -1 && listIndex > dividerResultIndex ? listIndex - 1 : listIndex,
    [dividerResultIndex],
  );

  // A search result carries no account/folder context for opening -- it's
  // resolved from the message itself, then handed to the mail view's own
  // selection atoms exactly like the old search page did. Navigating with
  // the fully-formed URL (rather than a bare "/") means the mail view's
  // own url-sync effect finds its cold-read already matching these atoms
  // on mount, and does not push a second history entry on top of this one.
  // Where a hit lands -- its folder wherever it sits, however far down,
  // or a recent unified view holding that folder -- is the same decision
  // every other "open this message" entry point makes: use-open-message.ts.
  const { openMessageById } = useOpenMessage();
  const openResult = useMutation({
    mutationFn: (messageId: string) => openMessageById(messageId),
  });

  // One string identifying exactly this search -- keys the VList (so a
  // genuinely new query starts at the top rather than inheriting the
  // previous query's scroll offset, clamped) and ties a persisted scroll
  // anchor/cache to the search it was captured under, so a return to a
  // DIFFERENT search can never apply a stale one left over from another.
  const listIdentity = useMemo(
    () =>
      [
        view.source,
        query,
        searchAccountId ?? "all",
        (folderIds ?? []).slice().sort().join(","),
        view.source === "text" ? [...fields].sort().join(",") : "",
        windowKey(timeWindow),
      ].join("|"),
    [view.source, query, searchAccountId, folderIds, fields, timeWindow],
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
    handle.scrollToIndex(listIndexOf(index), { align: "start" });
    ownWriteOffsetRef.current = handle.scrollOffset;
  }, [listIndexOf]);

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
      const index = resultIndexOf(handle.findItemIndex(offset));
      const row = results[index];
      if (!row) return;
      setScrollAnchor((prev) =>
        prev?.messageId === row.id && prev.listIdentity === listIdentity
          ? prev
          : { listIdentity, messageId: row.id },
      );
      setScrollCache({ listIdentity, cache: handle.cache });
    },
    [results, listIdentity, resultIndexOf, setScrollAnchor, setScrollCache],
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
        view.source === "text" &&
        hasNextPage &&
        !isFetchingNextPage
      ) {
        fetchNextPage();
      }
    },
    [view.source, hasNextPage, isFetchingNextPage, fetchNextPage, persistAnchor],
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

  // A failed query reads as "No results found" otherwise -- a lie the user
  // will act on by rephrasing or giving up on the feature.
  const showError =
    !showEmptyPrompt && !showNoFolderSelected && !isLoading && view.error !== null &&
    results.length === 0;
  // The text results stay when only the search by meaning failed.
  const showErrorBesideResults =
    !showEmptyPrompt && !showNoFolderSelected && !isLoading && view.error !== null &&
    results.length > 0;
  const errorMessage = view.error || "Search failed.";
  const showNoResults =
    !showEmptyPrompt &&
    !showNoFolderSelected &&
    !isLoading &&
    !showError &&
    view.notice !== "searching" &&
    results.length === 0;

  return (
    <div className="flex h-full flex-col gap-3 p-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">Search</h1>
        {!showEmptyPrompt && !showNoFolderSelected && !isLoading && !showError && !view.muted && (
          <span className="text-xs text-muted-foreground" data-testid="search-result-count">
            {total} {total === 1 ? "result" : "results"}
          </span>
        )}
      </div>

      <div className="flex items-center gap-2">
        <div className="relative min-w-0 flex-1">
          <SearchIcon className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={rawQuery}
            onChange={(e) => setRawQuery(e.target.value)}
            placeholder="Search messages…"
            className="pl-9"
          />
        </div>
        <TimePicker value={timeWindow} onChange={setTimeWindow} />
        <label className="flex items-center gap-2 whitespace-nowrap text-xs text-muted-foreground">
          <Switch checked={semantic} onCheckedChange={setSemantic} />
          Semantic search
        </label>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        {(accounts?.length ?? 0) > 1 && (
          <Select
            value={searchScopeAccountId ?? ALL_ACCOUNTS_VALUE}
            onValueChange={(v) =>
              setSearchScopeAccountId(v === ALL_ACCOUNTS_VALUE ? null : (v ?? null))
            }
          >
            <SelectTrigger className="w-auto gap-1.5" size="sm">
              {/* The underlying control only resolves a label itself when
                  given an item list, which nothing here passes -- without
                  this it renders the account's raw id. */}
              <SelectValue placeholder="All accounts">
                {(v: string) =>
                  v === ALL_ACCOUNTS_VALUE
                    ? "All accounts"
                    : (accounts?.find((a) => a.id === v)?.name ?? "All accounts")
                }
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_ACCOUNTS_VALUE}>All accounts</SelectItem>
              {accounts?.map((account) => (
                <SelectItem key={account.id} value={account.id}>
                  {account.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}

        <FolderPicker selectedIds={folderIds} onChange={setFolderIds} accountId={searchAccountId} />

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

        {timeWindow && <TimeWindowChip value={timeWindow} onClear={() => setTimeWindow(null)} />}
      </div>

      <div className="flex flex-1 flex-col overflow-hidden rounded-lg border">
        {/* Static text, no animation: the text results below stand in until the
            search by meaning lands. */}
        {view.notice === "searching" && !isLoading && (
          <div
            className="shrink-0 border-b px-4 py-1.5 text-xs text-muted-foreground"
            data-testid="search-by-meaning-notice"
          >
            Searching by meaning…
          </div>
        )}
        {view.notice === "unavailable" && !isLoading && (
          <div
            className="shrink-0 border-b px-4 py-1.5 text-xs text-muted-foreground"
            data-testid="search-semantic-unavailable"
          >
            Semantic search is unavailable right now
          </div>
        )}
        {showErrorBesideResults && (
          <div
            className="shrink-0 border-b px-4 py-1.5 text-xs text-muted-foreground"
            data-testid="search-semantic-failed"
          >
            Search by meaning failed: {errorMessage}
          </div>
        )}
        {isLoading && (
          <div className="flex flex-col gap-3 p-4">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-16 w-full" />
            ))}
          </div>
        )}

        {showEmptyPrompt && !isLoading && (
          <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
            <SearchIcon className="h-12 w-12 opacity-50" />
            <p>Enter at least 2 characters to search</p>
          </div>
        )}

        {showNoFolderSelected && (
          <div
            className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-muted-foreground"
            data-testid="search-no-folder-selected"
          >
            <FolderIcon className="h-12 w-12 opacity-50" />
            <p>Select at least one folder to search</p>
          </div>
        )}

        {showError && (
          <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
            <AlertCircle className="h-12 w-12 opacity-50" />
            <p>{errorMessage}</p>
          </div>
        )}

        {showNoResults && (
          <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-muted-foreground">
            <SearchIcon className="h-12 w-12 opacity-50" />
            <p>No results found</p>
          </div>
        )}

        {!isLoading && results.length > 0 && (
          <VList
            key={listIdentity}
            id="search-results-list"
            ref={vlistRef}
            className={cn("min-h-0 flex-1", view.muted && "opacity-60")}
            itemSize={72}
            cache={restorableCache}
            onScroll={handleScroll}
          >
            {[
              ...results.flatMap((result, i) => {
                const row = (
                  <SearchResultRow key={result.id} result={result} onOpen={openResult.mutate} />
                );
                return i === dividerResultIndex
                  ? [
                      <div
                        key={LESS_LIKELY_KEY}
                        data-testid="search-less-likely"
                        className="border-b bg-muted/40 px-4 py-1.5 text-xs font-medium text-muted-foreground"
                      >
                        Less likely
                      </div>,
                      row,
                    ]
                  : [row];
              }),
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
