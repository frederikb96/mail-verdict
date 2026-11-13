"use client";

/**
 * Notifications: whether this device is told of new mail even with no
 * MailVerdict page open (Web Push), which folders raise one, how the bell
 * counts, and every other device that has turned push on. Permission is
 * requested only on an explicit click -- never automatically on page load,
 * which every browser would refuse silently or ignore anyway (a permission
 * prompt not triggered by a user gesture).
 */

import { useState } from "react";
import { useAtom } from "jotai";
import { Bell, BellOff, FolderOpen, Loader2, Monitor, MoreVertical, Smartphone } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { CategoryHeader, SaveBar, SectionCard } from "@/components/settings/blocks";
import { FolderPicker } from "@/components/settings/folder-picker";
import { SettingFields, type FieldSpec } from "@/components/settings/setting-fields";
import {
  PUSH_CHANNELS,
  type PushChannel,
  alertEnabledFolderIdsAtom,
  isArrivalFolder,
} from "@/lib/alert-prefs";
import type { PushSubscriptionResponse } from "@/types/api";
import { useAccounts } from "@/hooks/use-accounts";
import { useSearchFolders } from "@/hooks/use-search-folders";
import { useSettingsDraft } from "@/hooks/use-settings-draft";
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

const CHANNEL_LABELS: Record<PushChannel, string> = { mail: "New mail", system: "System" };

const BELL: FieldSpec[] = [
  {
    category: "mail",
    key: "bell_badge_counts_new_mail",
    label: "Count new mail on the bell",
    help: "Off, the badge counts system notifications only; the bell's own list still shows every alert.",
    control: "switch",
  },
  {
    category: "mail",
    key: "notify_wait_seconds",
    label: "Wait before notifying",
    help: "A new-mail alert waits this long for your rules to refile the message, so it names the folder the mail ends up in.",
    control: "select",
    options: [
      { value: 0, label: "Immediately" },
      { value: 30, label: "30 seconds" },
      { value: 120, label: "2 minutes" },
      { value: 300, label: "5 minutes" },
    ],
  },
];

const BELL_CATEGORIES = ["mail"];

function ThisDeviceCard() {
  const { permission } = useNotificationPermission();
  const pushSupported = usePushSupported();
  const { subscription: mySubscription, isStale } = useMyPushSubscription();
  const enablePush = useEnablePush();
  const disablePush = useDisablePush();

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
    <SectionCard icon={Monitor} title="This device">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="text-sm text-muted-foreground">{statusText}</div>
        {(permission === "default" || isStale || (!mySubscription && permission === "granted")) &&
          permission !== "denied" && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => enablePush.mutate()}
              disabled={enablePush.isPending}
              className="shrink-0 gap-1.5 self-start"
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
            className="shrink-0 gap-1.5 self-start"
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
    </SectionCard>
  );
}

function FoldersCard() {
  const [localFolderIds, setLocalFolderIds] = useAtom(alertEnabledFolderIdsAtom);
  const { subscription: mySubscription } = useMyPushSubscription();
  const { options: everyFolder } = useSearchFolders();
  const { data: accounts } = useAccounts();
  const updatePrefs = useUpdatePushSubscription();
  // The same folder-visibility preference the sidebar hides Exchange's
  // non-mail folders (Kalender, Kontakte, Journal, Aufgaben, ...) with --
  // a folder nobody sees in the mail view is not one anyone means to pick
  // here either.
  const options = everyFolder.filter((o) => o.folder.is_visible);

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

  // What an unset preference resolves to -- the folders mail actually
  // arrives in, not Sent/Drafts/Trash/Junk -- the same default
  // useEffectiveAlertFolderIds computes, so the picker a person opens
  // shows exactly what's already in effect rather than starting them
  // from a different picture of "the default".
  const defaultIds = options
    .filter((o) => isArrivalFolder(o.folder.special_use))
    .map((o) => o.folder.id);
  const effectiveEnabled = enabledFolderIds === null ? defaultIds : enabledFolderIds;
  const isDefaultSet = (ids: string[]) =>
    ids.length === defaultIds.length && defaultIds.every((id) => ids.includes(id));

  const [customizing, setCustomizing] = useState(enabledFolderIds !== null);

  // Collapses back to null (the default arrival-folder scope) once the
  // ticked set matches it exactly, so a folder created later that also
  // isn't Sent/Drafts/Trash/Junk is included by default rather than
  // silently left out.
  const change = (next: string[]) => setEnabledFolderIds(isDefaultSet(next) ? null : next);

  const customized = enabledFolderIds !== null;
  const summary = customized
    ? `${effectiveEnabled.length} folder${effectiveEnabled.length === 1 ? "" : "s"} chosen`
    : "Inbox and your own folders, in every account (the default)";

  return (
    <SectionCard icon={FolderOpen} title="Which folders">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="text-sm text-muted-foreground" data-testid="notify-folder-summary">
          {summary}
        </div>
        <div className="flex shrink-0 items-center gap-2 self-start">
          {customized && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setEnabledFolderIds(null);
                setCustomizing(false);
              }}
            >
              Reset to default
            </Button>
          )}
          <Button
            variant="outline"
            size="sm"
            aria-expanded={customizing}
            onClick={() => setCustomizing((v) => !v)}
          >
            {customizing ? "Done" : "Customize"}
          </Button>
        </div>
      </div>
      {customizing && (
        <FolderPicker
          value={effectiveEnabled}
          onChange={change}
          accounts={(accounts ?? []).filter((a) => a.is_active)}
        />
      )}
    </SectionCard>
  );
}

function BellCard() {
  const draft = useSettingsDraft();
  return (
    <>
      <SectionCard icon={Bell} title="Bell">
        {draft.ready ? <SettingFields draft={draft} fields={BELL} /> : null}
      </SectionCard>
      {draft.errors.mail && (
        <p role="alert" className="text-sm text-destructive">
          Could not save mail settings: {draft.errors.mail}
        </p>
      )}
      <SaveBar
        dirtyCount={draft.count(BELL_CATEGORIES)}
        saving={draft.saving}
        onSave={() => draft.save(BELL_CATEGORIES)}
        onDiscard={() => draft.discard(BELL_CATEGORIES)}
      />
    </>
  );
}

function OtherDevicesCard() {
  const { subscription: mySubscription } = useMyPushSubscription();
  const { data: allSubscriptions } = usePushSubscriptions();
  const removeDevice = useRemovePushDevice();
  const updatePrefs = useUpdatePushSubscription();
  const [removing, setRemoving] = useState<PushSubscriptionResponse | null>(null);

  const otherDevices = (allSubscriptions ?? []).filter((s) => s.id !== mySubscription?.id);
  if (otherDevices.length === 0) return null;

  // A phone mutes a whole channel rather than folders -- its folder scope
  // is set on the phone itself.
  const setChannelMuted = (
    device: PushSubscriptionResponse, channel: PushChannel, muted: boolean,
  ) => {
    const next = new Set(device.muted_channels);
    if (muted) next.add(channel);
    else next.delete(channel);
    updatePrefs.mutate({ subscriptionId: device.id, data: { muted_channels: Array.from(next) } });
  };

  return (
    <SectionCard icon={Smartphone} title="Other devices">
      <div className="flex flex-col gap-2">
        {otherDevices.map((device) => {
          const isPhone = device.transport === "apns";
          const label = device.label ?? (isPhone ? "iPhone" : "Unnamed device");
          return (
            <div
              key={device.id}
              data-testid="push-device"
              className="flex flex-col gap-1.5 rounded-lg border px-3 py-2 text-sm"
            >
              <div className="flex items-center gap-2">
                {isPhone ? (
                  <Smartphone className="h-4 w-4 shrink-0 text-muted-foreground" />
                ) : (
                  <Monitor className="h-4 w-4 shrink-0 text-muted-foreground" />
                )}
                <span className="min-w-0 flex-1 truncate">{label}</span>
                {device.failed_at && (
                  <span className="shrink-0 text-xs text-destructive">unreachable</span>
                )}
                <span className="shrink-0 text-xs text-muted-foreground">
                  {device.last_seen_at ? formatRelativeDate(device.last_seen_at) : "never"}
                </span>
                <DropdownMenu>
                  <DropdownMenuTrigger
                    render={
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 shrink-0"
                        aria-label={`Actions for ${label}`}
                      />
                    }
                  >
                    <MoreVertical className="h-4 w-4" />
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    <DropdownMenuItem variant="destructive" onClick={() => setRemoving(device)}>
                      Remove device
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
              {isPhone && (
                <div className="flex items-center gap-4 pl-6 text-xs">
                  {PUSH_CHANNELS.map((channel) => (
                    <label key={channel} className="flex cursor-pointer items-center gap-1.5">
                      <Checkbox
                        checked={!device.muted_channels.includes(channel)}
                        onCheckedChange={(checked) =>
                          setChannelMuted(device, channel, checked !== true)
                        }
                      />
                      {CHANNEL_LABELS[channel]}
                    </label>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(open) => {
          if (!open) setRemoving(null);
        }}
        title={`Remove ${removing?.label ?? "this device"}?`}
        description="It stops receiving notifications until it turns them on again."
        confirmLabel="Remove device"
        isConfirming={removeDevice.isPending}
        onConfirm={() => {
          if (removing) removeDevice.mutate(removing.id, { onSettled: () => setRemoving(null) });
        }}
      />
    </SectionCard>
  );
}

export function NotificationsSettings() {
  return (
    <>
      <CategoryHeader title="Notifications" />
      <ThisDeviceCard />
      <FoldersCard />
      <BellCard />
      <OtherDevicesCard />
    </>
  );
}
