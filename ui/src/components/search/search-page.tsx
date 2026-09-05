"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation } from "@tanstack/react-query";
import { useAtom, useAtomValue, useSetAtom } from "jotai";
import { VList, type VListHandle } from "virtua";
import { AlertCircle, Loader2, Search as SearchIcon } from "lucide-react";

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
  searchSemanticModeAtom,
} from "@/lib/search-prefs";
import {
  selectedAccountIdAtom,
  selectedFolderIdAtom,
  selectedMailIdAtom,
  isUnifiedViewAtom,
} from "@/lib/atoms";
import type { SearchField } from "@/types/api";

const FIELD_LABELS: Record<SearchField, string> = {
  subject: "Subject",
  from: "From",
  to: "To",
  body: "Body",
};

/** How near the bottom, in px, triggers the next page -- same threshold
 * the mail list and contact list use for their own VList. */
const LOAD_MORE_MARGIN = 200;

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

  const vlistRef = useRef<VListHandle>(null);

  // A search over thousands of unindexed rows shouldn't fire on every
  // keystroke -- debounce like the compose recipient search does
  // (use-contacts.ts's useContactSearch, 200ms).
  useEffect(() => {
    const timer = setTimeout(() => setQuery(rawQuery), 250);
    return () => clearTimeout(timer);
  }, [rawQuery]);

  const searchAccountId = isUnified ? undefined : (selectedAccountId ?? undefined);
  const { data, isLoading, isFetchingNextPage, hasNextPage, fetchNextPage, isError, error } =
    useSearchResults({
      query,
      accountId: searchAccountId,
      folderIds,
      fields,
      semantic,
    });

  // The picker's own options, scoped the same way the search itself is --
  // an explicit folder selection made under a different account can name
  // no folder visible here, which the server ANDs into a query that can
  // never match anything, presented as an ordinary "No results found"
  // rather than the account-mismatch it actually is.
  const { options: scopedFolderOptions, isLoading: scopedFoldersLoading } =
    useSearchFolders(searchAccountId);
  useEffect(() => {
    if (folderIds === null || scopedFoldersLoading) return;
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

  const handleScroll = useCallback(
    (offset: number) => {
      if (!vlistRef.current) return;
      const { scrollSize, viewportSize } = vlistRef.current;
      if (
        scrollSize - offset - viewportSize < LOAD_MORE_MARGIN &&
        hasNextPage &&
        !isFetchingNextPage
      ) {
        fetchNextPage();
      }
    },
    [hasNextPage, isFetchingNextPage, fetchNextPage],
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

  const showEmptyPrompt = query.trim().length < 2;
  // A failed query (a semantic search with no provider configured, most
  // commonly) reads as "No results found" otherwise -- a lie the user
  // will act on by rephrasing or giving up on the feature.
  const showError = !showEmptyPrompt && isError;
  const showNoResults = !showEmptyPrompt && !isLoading && !showError && results.length === 0;
  const errorMessage =
    semantic && error instanceof ApiError && error.status === 503
      ? "Semantic search is unavailable -- no AI provider is configured for it."
      : error?.message || "Search failed.";

  return (
    <div className="flex h-full flex-col gap-3 p-4">
      <h1 className="text-xl font-semibold">Search</h1>

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
          <VList ref={vlistRef} className="h-full" itemSize={72} onScroll={handleScroll}>
            {results.map((result) => (
              <SearchResultRow
                key={result.message_id}
                result={result}
                onOpen={openResult.mutate}
              />
            ))}
          </VList>
        )}

        {isFetchingNextPage && (
          <div className="flex items-center justify-center py-3">
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
          </div>
        )}
      </div>
    </div>
  );
}
