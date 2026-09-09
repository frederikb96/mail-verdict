/**
 * Web Push subscription lifecycle: registering this browser as a device,
 * turning it back off, and reading/writing its own per-device
 * preferences (which folders alert, whether reminders do).
 *
 * `mailverdict:alerts.subscription-id` in localStorage is the one thing
 * kept client-side -- just enough for this browser to find its own row
 * again. Everything else (folder scope, reminders, label, when it was
 * last seen) lives on the server, in push_subscriptions, and is read
 * through TanStack like any other server state -- see alert-prefs.ts for
 * why that split is deliberate.
 */

import { useCallback, useEffect, useState } from "react";
import { useAtomValue } from "jotai";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { alertEnabledFolderIdsAtom } from "@/lib/alert-prefs";
import type { PushSubscriptionResponse } from "@/types/api";

const SUBSCRIPTION_ID_KEY = "mailverdict:alerts.subscription-id";

export const pushSubscriptionKeys = {
  list: ["push-subscriptions"] as const,
};

function readStoredSubscriptionId(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(SUBSCRIPTION_ID_KEY);
}

function writeStoredSubscriptionId(id: string | null) {
  if (typeof window === "undefined") return;
  if (id === null) window.localStorage.removeItem(SUBSCRIPTION_ID_KEY);
  else window.localStorage.setItem(SUBSCRIPTION_ID_KEY, id);
}

/** Whether this browser could possibly support Web Push at all -- distinct
 * from Notification permission, which is asked for separately and only
 * on an explicit click. Most notably false in Safari on iOS/iPadOS
 * unless the site has been installed to the home screen. */
export function isPushSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

/** The same check as isPushSupported(), but settled after mount rather
 * than read directly during render -- every page here is prerendered to
 * static HTML with no `window`, so a component calling isPushSupported()
 * straight in its render body renders "false" at build time and "true"
 * on this browser's very first client render, and React discards the
 * whole hydrated tree over the mismatch (the same family of bug
 * useIsMobile() and the theme provider already have to guard against in
 * this application). Starting false and flipping it in an effect is what
 * keeps the first client render identical to the prerendered one. */
export function usePushSupported(): boolean {
  const [supported, setSupported] = useState(false);
  useEffect(() => setSupported(isPushSupported()), []);
  return supported;
}

export function usePushSubscriptions() {
  return useQuery<PushSubscriptionResponse[]>({
    queryKey: pushSubscriptionKeys.list,
    queryFn: () => api.alerts.listSubscriptions(),
    staleTime: 10_000,
  });
}

/** This browser's own subscription row, derived from the stored id plus
 * the fetched list -- undefined while loading, null once loaded if this
 * browser has never subscribed or its subscription is gone (the push
 * service's own 404/410 signal deleted the row server-side; see
 * push/send.py). The two are told apart by `isLoading` at the call site,
 * the same convention every other TanStack-backed hook here follows. */
export function useMyPushSubscription(): {
  subscription: PushSubscriptionResponse | null | undefined;
  isLoading: boolean;
  isStale: boolean;
} {
  const { data, isLoading } = usePushSubscriptions();
  const storedId = readStoredSubscriptionId();
  if (isLoading) return { subscription: undefined, isLoading: true, isStale: false };
  if (!storedId) return { subscription: null, isLoading: false, isStale: false };
  const match = (data ?? []).find((s) => s.id === storedId);
  if (match) return { subscription: match, isLoading: false, isStale: false };
  // This browser believes it is registered, but the row is gone -- the
  // push service told the server this endpoint will never accept
  // another push. Reported as "stale" rather than "never enabled" so
  // Settings can say what actually happened.
  return { subscription: null, isLoading: false, isStale: true };
}

function guessDeviceLabel(): string {
  if (typeof navigator === "undefined") return "This device";
  const ua = navigator.userAgent;
  const browser = /Edg\//.test(ua)
    ? "Edge"
    : /Firefox\//.test(ua)
      ? "Firefox"
      : /Chrome\//.test(ua)
        ? "Chrome"
        : /Safari\//.test(ua)
          ? "Safari"
          : "Browser";
  const platform = /Android/.test(ua)
    ? "Android"
    : /iPhone|iPad|iPod/.test(ua)
      ? "iOS"
      : /Mac OS X/.test(ua)
        ? "Mac"
        : /Windows/.test(ua)
          ? "Windows"
          : /Linux/.test(ua)
            ? "Linux"
            : "";
  return platform ? `${browser} on ${platform}` : browser;
}

function urlBase64ToUint8Array(base64String: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = window.atob(base64);
  const bytes = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; i++) bytes[i] = rawData.charCodeAt(i);
  return bytes;
}

export type EnablePushResult =
  | { ok: true }
  | { ok: false; reason: "unsupported" | "denied" | "server-unavailable" };

/** The whole subscribe flow: request permission if not yet decided,
 * register the service worker, fetch this server's VAPID key, subscribe
 * through the browser's PushManager, and register the result with the
 * server. Never throws -- every way this can fail is a real, distinct
 * state Settings needs to show rather than an unhandled rejection. */
export function useEnablePush() {
  const queryClient = useQueryClient();
  return useMutation<EnablePushResult, Error, void>({
    mutationFn: async () => {
      if (typeof window === "undefined" || !("Notification" in window)) {
        return { ok: false, reason: "unsupported" };
      }

      let permission = Notification.permission;
      if (permission === "default") permission = await Notification.requestPermission();
      if (permission !== "granted") return { ok: false, reason: "denied" };

      if (!isPushSupported()) {
        // Notification permission alone is enough for the in-app path
        // (see use-sse.ts) -- this browser has no PushManager/service
        // worker to register with (most notably Safari on iOS/iPadOS
        // unless installed to the home screen), so there is nothing
        // further to do.
        return { ok: true };
      }

      const keyInfo = await api.alerts.vapidPublicKey();
      if (!keyInfo.available || !keyInfo.public_key) {
        return { ok: false, reason: "server-unavailable" };
      }

      const registration = await navigator.serviceWorker.register("/sw.js");
      await navigator.serviceWorker.ready;

      let pushSubscription = await registration.pushManager.getSubscription();
      if (!pushSubscription) {
        pushSubscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(keyInfo.public_key),
        });
      }
      const json = pushSubscription.toJSON();
      if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) {
        return { ok: false, reason: "server-unavailable" };
      }

      const row = await api.alerts.registerSubscription({
        endpoint: json.endpoint,
        keys: { p256dh: json.keys.p256dh, auth: json.keys.auth },
        label: guessDeviceLabel(),
      });
      writeStoredSubscriptionId(row.id);
      return { ok: true };
    },
    onSuccess: (result) => {
      if (result.ok) queryClient.invalidateQueries({ queryKey: pushSubscriptionKeys.list });
    },
  });
}

/** Turns push back off for this device: unsubscribes the browser's own
 * PushManager registration and removes the server-side row. Safe to
 * call even if the local id is stale (the row already gone) -- the
 * DELETE is a no-op then, and the stored id is cleared either way. */
export function useDisablePush() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const storedId = readStoredSubscriptionId();
      if ("serviceWorker" in navigator) {
        const registration = await navigator.serviceWorker.getRegistration("/");
        const sub = await registration?.pushManager.getSubscription();
        await sub?.unsubscribe();
      }
      if (storedId) await api.alerts.deleteSubscription(storedId);
      writeStoredSubscriptionId(null);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: pushSubscriptionKeys.list });
    },
  });
}

/** Removing a device from the list -- this one or another one. Only this
 * device's own PushManager can be unsubscribed from a browser, so a
 * different device's row is simply deleted server-side; it stops
 * receiving push the next time anything is sent to it, the same
 * cleanup a 404/410 response performs on its own. */
export function useRemovePushDevice() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (subscriptionId: string) => {
      const isThisDevice = subscriptionId === readStoredSubscriptionId();
      if (isThisDevice && "serviceWorker" in navigator) {
        const registration = await navigator.serviceWorker.getRegistration("/");
        const sub = await registration?.pushManager.getSubscription();
        await sub?.unsubscribe();
      }
      await api.alerts.deleteSubscription(subscriptionId);
      if (isThisDevice) writeStoredSubscriptionId(null);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: pushSubscriptionKeys.list });
    },
  });
}

export function useUpdatePushSubscription() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      subscriptionId,
      data,
    }: {
      subscriptionId: string;
      data: { alert_folder_ids?: string[] | null; reminders_enabled?: boolean };
    }) => api.alerts.updateSubscription(subscriptionId, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: pushSubscriptionKeys.list });
    },
  });
}

/** The folder scope actually in effect for this browser: a subscribed
 * device's own alert_folder_ids (the server-side authority its
 * preference lives on -- see the model's own docstring) once it has
 * one, the localStorage atom otherwise. One function computing this is
 * what keeps the SSE handler and the settings panel from drifting apart
 * on which of the two sources is authoritative right now. */
export function useEffectiveAlertFolderIds(): string[] | null {
  const localFolderIds = useAtomValue(alertEnabledFolderIdsAtom);
  const { subscription } = useMyPushSubscription();
  if (subscription) return subscription.alert_folder_ids;
  return localFolderIds;
}

/** Notification.permission read once on mount and refreshed by whatever
 * triggers a permission change in this tab -- SSR has no Notification
 * global, so this starts "default" and settles on the client. */
export function useNotificationPermission() {
  const [permission, setPermission] = useState<NotificationPermission | "unsupported">(
    "default",
  );
  const refresh = useCallback(() => {
    setPermission(typeof window !== "undefined" && "Notification" in window
      ? Notification.permission
      : "unsupported");
  }, []);
  useEffect(refresh, [refresh]);
  return { permission, refresh };
}
