"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation } from "@tanstack/react-query";
import { useAtomValue, useSetAtom } from "jotai";
import { Search as SearchIcon } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";

import { api } from "@/lib/api";
import { useSearch } from "@/hooks/use-search";
import { formatRelativeDate, extractSenderName } from "@/lib/format";
import {
  selectedAccountIdAtom,
  selectedFolderIdAtom,
  selectedMailIdAtom,
  isUnifiedViewAtom,
} from "@/lib/atoms";

/**
 * The API highlights matches with `**...**` (already stripped of any HTML
 * tags server-side isn't guaranteed, so tags are stripped here too). Split
 * on the markers and render the matched spans as actual bold text instead
 * of leaving the literal asterisks in the snippet.
 */
function renderSnippet(snippet: string) {
  const plain = snippet.replace(/<\/?[^>]+>/g, "");
  const parts = plain.split(/\*\*(.+?)\*\*/g);
  return parts.map((part, i) =>
    i % 2 === 1 ? <strong key={i}>{part}</strong> : part,
  );
}

export function SearchPage() {
  const [query, setQuery] = useState("");
  const router = useRouter();
  const selectedAccountId = useAtomValue(selectedAccountIdAtom);
  const isUnified = useAtomValue(isUnifiedViewAtom);
  const setSelectedAccountId = useSetAtom(selectedAccountIdAtom);
  const setSelectedFolderId = useSetAtom(selectedFolderIdAtom);
  const setSelectedMailId = useSetAtom(selectedMailIdAtom);

  // In unified view, search all accounts (no filter). Otherwise filter by selected account.
  const searchAccountId = isUnified ? undefined : (selectedAccountId ?? undefined);
  const { data, isLoading } = useSearch(query, searchAccountId);

  // A search result carries no account/folder -- resolve it from the message
  // itself when opened, then land on the mail view with it selected.
  const openResult = useMutation({
    mutationFn: (messageId: string) => api.mails.get(messageId),
    onSuccess: (mail) => {
      setSelectedAccountId(mail.account_id);
      setSelectedFolderId(mail.folder_id);
      setSelectedMailId(mail.id);
      router.push("/");
    },
  });

  return (
    <div className="flex flex-col gap-6 p-6">
      <h1 className="text-2xl font-semibold">Search</h1>

      <div className="relative">
        <SearchIcon className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search messages..."
          className="pl-9"
        />
      </div>

      {isLoading && (
        <div className="flex flex-col gap-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-20 w-full" />
          ))}
        </div>
      )}

      {data && (
        <div className="flex flex-col gap-2">
          <div className="text-sm text-muted-foreground">
            {data.total} result{data.total !== 1 ? "s" : ""} for &ldquo;{data.query}&rdquo;
          </div>
          {data.results.length === 0 && (
            <div className="flex flex-col items-center gap-3 py-12 text-muted-foreground">
              <SearchIcon className="h-12 w-12 opacity-50" />
              <p>No results found</p>
            </div>
          )}
          {data.results.map((result) => (
            <Card
              key={result.message_id}
              role="button"
              tabIndex={0}
              aria-label={`Open ${result.subject ?? "(no subject)"}`}
              className="cursor-pointer transition-colors hover:bg-accent/50"
              onClick={() => openResult.mutate(result.message_id)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  openResult.mutate(result.message_id);
                }
              }}
            >
              <CardContent className="flex flex-col gap-1 py-3">
                <div className="flex items-center gap-2">
                  <span className="truncate font-medium">
                    {result.subject ?? "(no subject)"}
                  </span>
                  <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                    {formatRelativeDate(result.received_at)}
                  </span>
                </div>
                <div className="truncate text-sm text-muted-foreground">
                  {extractSenderName(result.from_addr)}
                </div>
                {result.snippet && (
                  <div className="truncate text-xs text-muted-foreground">
                    {renderSnippet(result.snippet)}
                  </div>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {!data && !isLoading && query.length < 2 && (
        <div className="flex flex-col items-center gap-3 py-12 text-muted-foreground">
          <SearchIcon className="h-12 w-12 opacity-50" />
          <p>Enter at least 2 characters to search</p>
        </div>
      )}
    </div>
  );
}
