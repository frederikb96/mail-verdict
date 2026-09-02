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
import { getInitials, parseAddressList } from "@/lib/format";

interface RecipientFieldProps {
  value: string[];
  onChange: (emails: string[]) => void;
  placeholder?: string;
}

export function RecipientField({ value, onChange, placeholder }: RecipientFieldProps) {
  const [query, setQuery] = useState("");
  const { results } = useContactSearch(query);

  const items = results.map((r) => r.email);

  const addAddresses = (raw: string) => {
    const parsed = parseAddressList(raw);
    if (parsed.length === 0) return;
    const merged = Array.from(new Set([...value, ...parsed]));
    onChange(merged);
    setQuery("");
  };

  return (
    <Combobox
      multiple
      items={items}
      value={value}
      onValueChange={(next) => onChange(next as string[])}
      inputValue={query}
      onInputValueChange={setQuery}
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
