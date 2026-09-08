"use client";

import { useEffect } from "react";
import { useSetAtom } from "jotai";

import { composeIntentAtom } from "@/lib/atoms";
import { parseMailto } from "@/lib/mailto";

/**
 * Opens the composer for a `mailto:` link the browser routed here.
 *
 * The manifest registers this app as a `mailto` handler and points at
 * `/?compose=<url>`, so an installed app is reachable as the system mail
 * client. The parameter is consumed and removed from the address bar: left
 * in place, a reload -- or a bookmark of the resulting URL -- would keep
 * reopening the same composer.
 *
 * `window.location` rather than `useSearchParams`, because this app is a
 * static export: the search params hook forces the whole subtree into a
 * Suspense boundary for a value that is only ever read once, at mount.
 */
export function ProtocolHandler() {
  const setComposeIntent = useSetAtom(composeIntentAtom);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const raw = params.get("compose");
    if (!raw) return;

    params.delete("compose");
    const query = params.toString();
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${query ? `?${query}` : ""}`,
    );

    const intent = parseMailto(raw);
    // An unparseable link still opens the composer, empty: the click was a
    // request to write a message, and dropping it silently looks like the
    // mail client failing to start.
    setComposeIntent(intent ?? {});
  }, [setComposeIntent]);

  return null;
}
