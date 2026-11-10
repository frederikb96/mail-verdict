"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

/** Ctrl+Alt+<n> jumps to a top-level section, the way desktop mail clients
 * number theirs. Keyed on `code` rather than `key`: with Alt held, a digit key
 * can report a layout-specific symbol (Ctrl+Alt is AltGr on Windows). */
const SECTIONS: Record<string, string> = {
  Digit1: "/",
  Digit2: "/calendar",
  Digit3: "/contacts",
};

export function SectionShortcuts() {
  const router = useRouter();

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!event.ctrlKey || !event.altKey || event.shiftKey || event.metaKey) return;
      const target = SECTIONS[event.code];
      if (!target) return;
      event.preventDefault();
      router.push(target);
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [router]);

  return null;
}
