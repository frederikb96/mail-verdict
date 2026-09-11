"use client";

/**
 * One bell for both durable, in-app record kinds: new mail (the Mail tab)
 * and a write PostIMAP gave up on permanently (the System tab). Neither
 * list is account-scoped -- Mail already wasn't (see use-alerts.ts), and
 * System is fanned out across every account, active or not (see
 * useAllAccountsNotifications) so a write failure on an account that
 * isn't currently selected -- or is disabled -- is never silently
 * invisible, and never blocks a folder-delete guard the bell itself
 * cannot explain.
 *
 * Both tabs are fetched unseen/unacknowledged-only, not a plain recent
 * page filtered client-side: a page capped smaller than what's actually
 * outstanding used to read as "nothing here" while the count (and, for
 * notifications, a folder-delete guard) disagreed -- see both hooks'
 * own comments.
 *
 * The badge and both tabs all read the same two counts computed below --
 * nowhere else re-derives "how many are unread". Which of them the badge
 * adds up, and which alert kinds are new mail rather than system
 * notifications (listed under System with the write failures), is
 * lib/bell-badge.ts's decision alone.
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Bell, CheckCheck, Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  useAlerts,
  useDismissAlert,
  useDismissAllAlerts,
  useUnseenAlertCount,
} from "@/hooks/use-alerts";
import {
  useAcknowledgeAllNotificationsEverywhere,
  useAcknowledgeNotification,
  useAllAccountsNotifications,
} from "@/hooks/use-notifications";
import { useAccounts } from "@/hooks/use-accounts";
import { useSettings } from "@/hooks/use-settings";
import { MAIL_ALERT_KIND, bellBadgeCount, isMailAlertKind } from "@/lib/bell-badge";
import { formatRelativeDate } from "@/lib/format";
import type { AlertResponse, NotificationResponse } from "@/types/api";

const ACTION_LABELS: Record<string, string> = {
  flag_add: "Setting a flag",
  flag_remove: "Clearing a flag",
  move: "Moving a message",
  delete: "Deleting a message",
  send: "Sending a message",
  draft: "Saving a draft",
};

function EmptyState({ text }: { text: string }) {
  return <div className="px-3 py-4 text-center text-sm text-muted-foreground">{text}</div>;
}

function AlertRow({
  alert,
  accountName,
  onOpen,
  onDismiss,
  isDismissing,
}: {
  alert: AlertResponse;
  accountName: string | null;
  onOpen: () => void;
  onDismiss: () => void;
  isDismissing: boolean;
}) {
  const unseen = alert.dismissed_at === null;
  return (
    <div
      data-testid="alert-row"
      data-alert-id={alert.id}
      className={`flex flex-col gap-1 border-b px-3 py-2 last:border-b-0 ${
        unseen ? "bg-accent/30" : ""
      }`}
    >
      <button type="button" className="flex flex-col gap-0.5 text-left" onClick={onOpen}>
        <div className="flex items-start justify-between gap-2">
          <span className="truncate text-sm font-medium">{alert.title || "(no subject)"}</span>
          <span className="shrink-0 text-xs text-muted-foreground">
            {formatRelativeDate(alert.delivered_at ?? alert.created_at)}
          </span>
        </div>
        {alert.body && <span className="truncate text-xs text-muted-foreground">{alert.body}</span>}
        {accountName && (
          <span className="shrink-0 self-start truncate rounded-full border px-1.5 py-0 text-[10px] text-muted-foreground">
            {accountName}
          </span>
        )}
      </button>
      {unseen && (
        <Button
          variant="ghost"
          size="sm"
          className="h-6 w-fit px-2 text-xs"
          disabled={isDismissing}
          onClick={onDismiss}
        >
          {isDismissing ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : null}
          Dismiss
        </Button>
      )}
    </div>
  );
}

function SystemRow({
  notification,
  accountName,
  onAcknowledge,
  isAcknowledging,
}: {
  notification: NotificationResponse;
  accountName: string | null;
  onAcknowledge: () => void;
  isAcknowledging: boolean;
}) {
  const detail = notification.detail ?? {};
  const subject = typeof detail.subject === "string" ? detail.subject : null;
  const stillApplied = notification.reverted_at === null;

  return (
    <div
      data-testid="system-row"
      data-notification-id={notification.id}
      className="flex flex-col gap-1 border-b px-3 py-2 last:border-b-0"
    >
      <div className="flex items-start justify-between gap-2">
        <span className="text-sm font-medium">
          {ACTION_LABELS[notification.action] ?? notification.action} failed
        </span>
        <span className="shrink-0 text-xs text-muted-foreground">
          {formatRelativeDate(notification.created_at)}
        </span>
      </div>
      {subject && (
        <span className="truncate text-xs text-muted-foreground">
          &ldquo;{subject}&rdquo;
        </span>
      )}
      {notification.error && (
        <span className="text-xs text-destructive">{notification.error}</span>
      )}
      {stillApplied && (
        <span className="text-xs text-muted-foreground">
          Our value is still shown as applied -- the server never got it.
        </span>
      )}
      {accountName && (
        <span className="shrink-0 self-start truncate rounded-full border px-1.5 py-0 text-[10px] text-muted-foreground">
          {accountName}
        </span>
      )}
      {notification.acknowledged_at === null && (
        <Button
          variant="ghost"
          size="sm"
          className="h-6 w-fit px-2 text-xs"
          disabled={isAcknowledging}
          onClick={onAcknowledge}
        >
          {isAcknowledging ? (
            <Loader2 className="mr-1 h-3 w-3 animate-spin" />
          ) : null}
          Dismiss
        </Button>
      )}
    </div>
  );
}

export function NotificationBell() {
  const router = useRouter();
  const [tab, setTab] = useState<"mail" | "system">("mail");

  const { data: alertCount } = useUnseenAlertCount();
  const { data: alerts, isLoading: alertsLoading } = useAlerts(200, { unseenOnly: true });
  const dismissAlert = useDismissAlert();
  const dismissAllAlerts = useDismissAllAlerts();

  const {
    notifications: unacknowledged, isLoading: notificationsLoading, unacknowledgedCount,
  } = useAllAccountsNotifications();
  const acknowledge = useAcknowledgeNotification();
  const acknowledgeAllEverywhere = useAcknowledgeAllNotificationsEverywhere();

  const { data: accounts } = useAccounts();
  const showAccount = (accounts?.length ?? 0) > 1;
  const accountName = (accountId: string | null) =>
    showAccount && accountId ? (accounts?.find((a) => a.id === accountId)?.name ?? null) : null;

  const { data: mailSettings } = useSettings("mail");

  // unacknowledgedCount is the account-wide server count (matching the
  // folder-delete guard's own predicate exactly), not unacknowledged's
  // own length -- see useAllAccountsNotifications for why the two can
  // differ.
  const countsNewMail = mailSettings?.bell_badge_counts_new_mail;
  const unseenAlertsByKind = alertCount?.by_kind ?? {};
  const badgeCount = bellBadgeCount({
    unseenAlertsByKind,
    unacknowledgedNotifications: unacknowledgedCount,
    countsNewMail: typeof countsNewMail === "boolean" ? countsNewMail : undefined,
  });
  const mailAlerts = (alerts ?? []).filter((a) => isMailAlertKind(a.kind));
  const systemAlerts = (alerts ?? []).filter((a) => !isMailAlertKind(a.kind));
  const systemAlertKinds = Object.keys(unseenAlertsByKind).filter((k) => !isMailAlertKind(k));

  const openAlert = (alert: AlertResponse) => {
    if (alert.dismissed_at === null) dismissAlert.mutate(alert.id);
    if (alert.url) router.push(alert.url);
  };

  return (
    <Popover>
      <PopoverTrigger
        render={<Button variant="ghost" size="icon" className="relative h-8 w-8" />}
        title="Notifications"
      >
        <Bell className="h-4 w-4" />
        {badgeCount > 0 && (
          <Badge
            variant="destructive"
            data-testid="bell-badge"
            className="absolute -right-1 -top-1 h-4 min-w-4 justify-center px-1 text-[10px]"
          >
            {badgeCount > 99 ? "99+" : badgeCount}
          </Badge>
        )}
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80 p-0">
        <div className="border-b px-3 py-2">
          <span className="text-sm font-medium">Notifications</span>
        </div>
        <Tabs value={tab} onValueChange={(v) => setTab(v as "mail" | "system")}>
          <div className="flex items-center justify-between border-b px-3 py-1.5">
            <TabsList>
              <TabsTrigger value="mail">Mail</TabsTrigger>
              <TabsTrigger value="system">System</TabsTrigger>
            </TabsList>
            {tab === "mail" && mailAlerts.length > 0 && (
              <Button
                variant="ghost"
                size="sm"
                className="h-6 gap-1 px-2 text-xs"
                disabled={dismissAllAlerts.isPending}
                onClick={() => dismissAllAlerts.mutate([MAIL_ALERT_KIND])}
              >
                <CheckCheck className="h-3 w-3" />
                Dismiss all
              </Button>
            )}
            {tab === "system" && (unacknowledged.length > 0 || systemAlerts.length > 0) && (
              <Button
                variant="ghost"
                size="sm"
                className="h-6 gap-1 px-2 text-xs"
                disabled={acknowledgeAllEverywhere.isPending || dismissAllAlerts.isPending}
                onClick={() => {
                  if (unacknowledged.length > 0) {
                    acknowledgeAllEverywhere.mutate(
                      Array.from(new Set(unacknowledged.map((n) => n.account_id))),
                    );
                  }
                  if (systemAlertKinds.length > 0) dismissAllAlerts.mutate(systemAlertKinds);
                }}
              >
                <CheckCheck className="h-3 w-3" />
                Dismiss all
              </Button>
            )}
          </div>
          <TabsContent value="mail" className="m-0">
            <div className="max-h-80 overflow-y-auto">
              {alertsLoading && <EmptyState text="Loading..." />}
              {!alertsLoading && mailAlerts.length === 0 && <EmptyState text="Nothing yet" />}
              {mailAlerts.map((alert) => (
                <AlertRow
                  key={alert.id}
                  alert={alert}
                  accountName={accountName(alert.account_id)}
                  onOpen={() => openAlert(alert)}
                  onDismiss={() => dismissAlert.mutate(alert.id)}
                  isDismissing={dismissAlert.isPending && dismissAlert.variables === alert.id}
                />
              ))}
            </div>
          </TabsContent>
          <TabsContent value="system" className="m-0">
            <div className="max-h-80 overflow-y-auto">
              {(notificationsLoading || alertsLoading) && <EmptyState text="Loading..." />}
              {!notificationsLoading && !alertsLoading && unacknowledged.length === 0 &&
                systemAlerts.length === 0 && <EmptyState text="Nothing to report" />}
              {systemAlerts.map((alert) => (
                <AlertRow
                  key={alert.id}
                  alert={alert}
                  accountName={accountName(alert.account_id)}
                  onOpen={() => openAlert(alert)}
                  onDismiss={() => dismissAlert.mutate(alert.id)}
                  isDismissing={dismissAlert.isPending && dismissAlert.variables === alert.id}
                />
              ))}
              {unacknowledged.map((n) => (
                <SystemRow
                  key={n.id}
                  notification={n}
                  accountName={accountName(n.account_id)}
                  isAcknowledging={
                    acknowledge.isPending && acknowledge.variables?.notificationId === n.id
                  }
                  onAcknowledge={() =>
                    acknowledge.mutate({ accountId: n.account_id, notificationId: n.id })
                  }
                />
              ))}
            </div>
          </TabsContent>
        </Tabs>
      </PopoverContent>
    </Popover>
  );
}
