"use client";

import { useEffect, useState, type ReactNode } from "react";

/**
 * Renders its children only once the browser has them.
 *
 * Every page here is prerendered to static HTML at build time, so anything
 * a component derives from the clock -- today, the current time, the date
 * a view is anchored on -- is baked from the build machine's own clock and
 * timezone. A browser in a different zone, or simply loading the page on a
 * later day, then hydrates against markup describing a different day:
 * React reports the mismatch and rebuilds the tree from scratch. Rendering
 * nothing until the client has mounted makes the prerendered output and
 * the first client render agree by construction, and costs nothing for a
 * view that has to wait for the API regardless.
 */
export function ClientOnly({ children }: { children: ReactNode }) {
  const [mounted, setMounted] = useState(false);

  useEffect(() => setMounted(true), []);

  return mounted ? <>{children}</> : null;
}
