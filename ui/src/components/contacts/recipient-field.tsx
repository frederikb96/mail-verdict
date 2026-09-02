"use client";

/**
 * The compose recipient autocomplete: chips for selected addresses, backed
 * by the server-filtered contact search (`filter={null}`, the server does
 * the matching), and creatable -- a typed address matching nothing becomes
 * a chip on Enter, comma, Tab or blur.
 */

import { useState } from "react";
import {
  Combobox,
  ComboboxChip,
  ComboboxChipRemove,
  ComboboxChips,
  ComboboxContent,
  ComboboxEmpty,
  ComboboxInput,
  ComboboxItem,
} from "@/components/ui/combobox";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { useContactSearch } from "@/hooks/use-contacts";
import { useToast } from "@/hooks/use-toast";
import { getInitials, isValidEmail, parseAddressList } from "@/lib/format";

interface RecipientFieldProps {
  value: string[];
  onChange: (emails: string[]) => void;
  placeholder?: string;
}

export function RecipientField({ value, onChange, placeholder }: RecipientFieldProps) {
  const [query, setQuery] = useState("");
  // Which suggestion, if any, the list is currently highlighting -- while
  // one is, Enter belongs to the combobox itself (commit that suggestion),
  // not to the free-text path below.
  const [highlighted, setHighlighted] = useState<string | undefined>(undefined);
  const { results } = useContactSearch(query);
  const { push: pushToast } = useToast();

  const items = results.map((r) => r.email);

  const addAddresses = (raw: string) => {
    const parsed = parseAddressList(raw);
    if (parsed.length === 0) return;
    const valid = parsed.filter(isValidEmail);
    const invalid = parsed.filter((a) => !isValidEmail(a));
    if (valid.length > 0) {
      onChange(Array.from(new Set([...value, ...valid])));
    }
    if (invalid.length > 0) {
      // Neither silently accepted nor silently dropped: named in a toast
      // rather than turned into a chip. The combobox clears the typed
      // text itself once the popup closes with nothing selected, the same
      // way it already does for a query that matched no suggestion.
      pushToast(
        `Not a valid email address: ${invalid.join(", ")}`,
        "warning",
      );
    }
  };

  return (
    <Combobox
      multiple
      items={items}
      value={value}
      onValueChange={(next) => onChange(next as string[])}
      inputValue={query}
      onInputValueChange={setQuery}
      onItemHighlighted={(v) => setHighlighted(v as string | undefined)}
      filter={null}
    >
      <ComboboxChips>
        {value.map((email) => (
          <ComboboxChip key={email}>
            {results.find((r) => r.email === email)?.name || email}
            <ComboboxChipRemove onClick={() => onChange(value.filter((v) => v !== email))} />
          </ComboboxChip>
        ))}
        <ComboboxInput
          placeholder={value.length === 0 ? placeholder : undefined}
          onKeyDown={(e) => {
            // A highlighted suggestion owns Enter/Tab/comma -- let the
            // combobox's own selection commit it rather than racing it
            // with the raw text underneath. Clearing the query ourselves
            // is still ours to do: the combobox only clears it on a mouse
            // click, not on this keyboard path.
            if (highlighted) {
              if (e.key === "Enter" || e.key === "," || e.key === "Tab") setQuery("");
              return;
            }
            if (e.key === "Enter" || e.key === "," || e.key === "Tab") {
              if (query.trim()) {
                e.preventDefault();
                addAddresses(query);
              }
            }
          }}
          onBlur={() => {
            if (query.trim()) addAddresses(query);
          }}
          onPaste={(e) => {
            const text = e.clipboardData.getData("text");
            if (text.includes(",") || text.includes(";")) {
              e.preventDefault();
              addAddresses(text);
            }
          }}
        />
      </ComboboxChips>
      <ComboboxContent>
        <ComboboxEmpty>{query.trim() ? "Press Enter to add this address" : "Type to search"}</ComboboxEmpty>
        {results.map((hit) => (
          <ComboboxItem key={hit.email} value={hit.email}>
            <Avatar size="sm">
              <AvatarFallback>{getInitials(hit.name || hit.email)}</AvatarFallback>
            </Avatar>
            <div className="flex min-w-0 flex-col">
              <span className="truncate">{hit.name || hit.email}</span>
              {hit.name && <span className="truncate text-xs text-muted-foreground">{hit.email}</span>}
            </div>
          </ComboboxItem>
        ))}
      </ComboboxContent>
    </Combobox>
  );
}
