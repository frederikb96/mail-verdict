"use client";

/**
 * Answers the service worker when a system notification is clicked while
 * this window is open: the URL is opened in place, through the same path an
 * alert clicked in the bell takes, instead of the window being navigated
 * and the whole application reloaded. Answering tells the worker not to
 * fall back to that navigation -- see public/sw.js.
 */

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";

import { useOpenMessage } from "@/hooks/use-open-message";

export function ServiceWorkerNavigation() {
  const router = useRouter();
  const { openMessageById } = useOpenMessage();
  // The listener is registered once; it calls whatever is current.
  const openRef = useRef(openMessageById);
  openRef.current = openMessageById;

  useEffect(() => {
    if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;
    const container = navigator.serviceWorker;

    const onMessage = (event: MessageEvent) => {
      const data = event.data as { type?: unknown; url?: unknown } | null;
      if (data?.type !== "mailverdict:open-url" || typeof data.url !== "string") return;
      const target = new URL(data.url, window.location.origin);
      if (target.origin !== window.location.origin) return;
      event.ports[0]?.postMessage("opened");

      const messageId = target.pathname === "/" ? target.searchParams.get("message") : null;
      if (messageId) void openRef.current(messageId);
      else router.push(`${target.pathname}${target.search}`);
    };

    container.addEventListener("message", onMessage);
    // Messages sent before a listener exists are queued until this runs.
    container.startMessages();
    return () => container.removeEventListener("message", onMessage);
  }, [router]);

  return null;
}
