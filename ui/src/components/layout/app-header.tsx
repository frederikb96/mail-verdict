"use client";

/**
 * The one header row every page shares -- previously just the sidebar
 * toggle and the connection indicator, the most valuable strip of screen
 * in the app saying nothing actionable. A global search field lives here
 * instead: `/` focuses it from anywhere, and Enter jumps to the full
 * search page with the query already applied, which stays the place for
 * scope, fields and semantic search rather than duplicating any of that
 * here.
 */

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Search } from "lucide-react";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { Input } from "@/components/ui/input";
import { ConnectionIndicator } from "@/components/layout/connection-indicator";
import { isEditableElement } from "@/lib/utils";

export function AppHeader() {
  const router = useRouter();
  const [value, setValue] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
      if (isEditableElement(e.target)) return;
      e.preventDefault();
      inputRef.current?.focus();
      inputRef.current?.select();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  const submit = () => {
    const q = value.trim();
    if (!q) return;
    router.push(`/search?q=${encodeURIComponent(q)}`);
  };

  return (
    <div className="flex items-center gap-2 border-b px-2 py-0.5">
      <SidebarTrigger />
      <div className="relative min-w-0 max-w-sm flex-1">
        <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input
          ref={inputRef}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              submit();
            } else if (e.key === "Escape") {
              setValue("");
              inputRef.current?.blur();
            }
          }}
          placeholder="Search mail…"
          aria-label="Search mail"
          className="h-7 pl-7 text-xs"
        />
      </div>
      <div className="ml-auto">
        <ConnectionIndicator />
      </div>
    </div>
  );
}
