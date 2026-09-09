"use client";

/**
 * Which folders raise a system notification, whether that reaches this
 * device even with no MailVerdict page open (Web Push), and the list of
 * every other device that has turned push on. Permission is requested
 * only on an explicit click -- never automatically on page load, which
 * every browser would refuse silently or ignore anyway (a permission
 * prompt not triggered by a user gesture).
 */

import { useAtom } from "jotai";
import { Bell, BellOff, Loader2, Smartphone, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { alertEnabledFolderIdsAtom } from "@/lib/alert-prefs";
import { useSearchFolders } from "@/hooks/use-search-folders";
import {
  usePushSupported,
  useDisablePush,
  useEnablePush,
  useMyPushSubscription,
  usePushSubscriptions,
  useNotificationPermission,
  useRemovePushDevice,
  useUpdatePushSubscription,
} from "@/hooks/use-push";
import { formatRelativeDate } from "@/lib/format";

export function AlertSettings() {
  const { permission } = useNotificationPermission();
  const pushSupported = usePushSupported();

  const [localFolderIds, setLocalFolderIds] = useAtom(alertEnabledFolderIdsAtom);
  const { subscription: mySubscription, isStale } = useMyPushSubscription();
  const { data: allSubscriptions } = usePushSubscriptions();
  const { options, isLoading } = useSearchFolders();

  const enablePush = useEnablePush();
  const disablePush = useDisablePush();
  const removeDevice = useRemovePushDevice();
  const updatePrefs = useUpdatePushSubscription();

  // A subscribed device's own folder scope is the server-side row (the
  // one true per-device authority once it exists); everything else
  // falls back to this browser's own localStorage preference -- the
  // same precedence use-sse.ts's useEffectiveAlertFolderIds computes,
  // duplicated here only because a control also needs to write it, not
  // merely read it.
  const enabledFolderIds = mySubscription ? mySubscription.alert_folder_ids : localFolderIds;
  const setEnabledFolderIds = (next: string[] | null) => {
    if (mySubscription) {
      updatePrefs.mutate({ subscriptionId: mySubscription.id, data: { alert_folder_ids: next } });
    } else {
      setLocalFolderIds(next);
    }
  };

  const allIds = options.map((o) => o.folder.id);
  const effectiveEnabled = enabledFolderIds === null ? allIds : enabledFolderIds;
  const enabledSet = new Set(effectiveEnabled);
  const allEnabled = allIds.length > 0 && effectiveEnabled.length === allIds.length;

  const groups = new Map<string, { accountName: string; options: typeof options }>();
  for (const opt of options) {
    const entry = groups.get(opt.accountId) ?? { accountName: opt.accountName, options: [] };
    entry.options.push(opt);
    groups.set(opt.accountId, entry);
  }

  const toggleFolder = (folderId: string, checked: boolean) => {
    const next = new Set(effectiveEnabled);
    if (checked) next.add(folderId);
    else next.delete(folderId);
    // Collapses back to null ("every folder") once everything is ticked,
    // the same convention search's own FolderPicker uses -- so a folder
    // created later is included by default rather than silently left out.
    setEnabledFolderIds(next.size >= allIds.length ? null : Array.from(next));
  };

  const otherDevices = (allSubscriptions ?? []).filter((s) => s.id !== mySubscription?.id);

  const statusText = (() => {
    if (permission === "unsupported") return "This browser does not support notifications.";
    if (permission === "denied") {
      return "Notifications are blocked for this site -- allow them in the browser's own site settings to turn this back on.";
    }
    if (mySubscription) {
      return "This device receives a notification for new mail even when MailVerdict isn't open.";
    }
    if (isStale) {
      return "Push notifications turned off automatically -- this browser's subscription is no longer valid.";
    }
    if (pushSupported) {
      return "Turn on notifications for new mail, including when MailVerdict isn't open.";
    }
    if (permission === "granted") {
      return "This browser will show a system notification for new mail while a tab is open.";
    }
    return "Turn on system notifications for new mail arriving while this tab isn't focused. This browser cannot deliver them while no tab is open.";
  })();

  const enableResult = enablePush.data;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Bell className="h-4 w-4" />
          Alerts
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex items-center justify-between gap-3">
          <div className="text-sm text-muted-foreground">{statusText}</div>
          {(permission === "default" || isStale || (!mySubscription && permission === "granted")) &&
            permission !== "denied" && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => enablePush.mutate()}
                disabled={enablePush.isPending}
                className="shrink-0 gap-1.5"
              >
                {enablePush.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Bell className="h-3.5 w-3.5" />
                )}
                Enable
              </Button>
            )}
          {mySubscription && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => disablePush.mutate()}
              disabled={disablePush.isPending}
              className="shrink-0 gap-1.5"
            >
              {disablePush.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <BellOff className="h-3.5 w-3.5" />
              )}
              Disable
            </Button>
          )}
          {permission === "denied" && (
            <span className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
              <BellOff className="h-3.5 w-3.5" />
              Off
            </span>
          )}
        </div>

        {enableResult && !enableResult.ok && enableResult.reason === "server-unavailable" && (
          <div className="text-xs text-destructive">
            This server has no push notifications configured yet.
          </div>
        )}

        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between text-xs">
            <span className="font-medium text-muted-foreground">Notify for these folders</span>
            <button
              type="button"
              className="text-primary hover:underline"
              onClick={() => setEnabledFolderIds(allEnabled ? [] : null)}
            >
              {allEnabled ? "Deselect all" : "Select all"}
            </button>
          </div>
          {isLoading && <div className="text-xs text-muted-foreground">Loading folders…</div>}
          {!isLoading && options.length === 0 && (
            <div className="text-xs text-muted-foreground">No folders yet</div>
          )}
          <div className="max-h-56 overflow-y-auto rounded-md border p-1">
            {Array.from(groups.values()).map((group) => (
              <div key={group.accountName}>
                {groups.size > 1 && (
                  <div className="px-2 pt-2 pb-1 text-xs font-medium text-muted-foreground">
                    {group.accountName}
                  </div>
                )}
                {group.options.map(({ folder }) => (
                  <label
                    key={folder.id}
                    className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-accent/50"
                  >
                    <Checkbox
                      checked={enabledSet.has(folder.id)}
                      onCheckedChange={(checked) => toggleFolder(folder.id, checked === true)}
                    />
                    <span className="truncate">{folder.display_name ?? folder.imap_name}</span>
                  </label>
                ))}
              </div>
            ))}
          </div>
        </div>

        {otherDevices.length > 0 && (
          <div className="flex flex-col gap-2">
            <span className="text-xs font-medium text-muted-foreground">Other devices</span>
            <div className="flex flex-col gap-1">
              {otherDevices.map((device) => (
                <div
                  key={device.id}
                  className="flex items-center justify-between gap-2 rounded-md border px-2 py-1.5 text-sm"
                >
                  <span className="flex min-w-0 items-center gap-2">
                    <Smartphone className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                    <span className="truncate">{device.label ?? "Unnamed device"}</span>
                    {device.failed_at && (
                      <span className="shrink-0 text-xs text-destructive">unreachable</span>
                    )}
                  </span>
                  <span className="flex shrink-0 items-center gap-2">
                    <span className="text-xs text-muted-foreground">
                      {device.last_seen_at ? formatRelativeDate(device.last_seen_at) : "never"}
                    </span>
                    <button
                      type="button"
                      title="Remove device"
                      className="text-muted-foreground hover:text-destructive"
                      onClick={() => removeDevice.mutate(device.id)}
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
