"use client";

/**
 * Notification centre: writes PostIMAP gave up on permanently for the
 * current account, including a send that never left. Built on
 * sync_notifications -- see docs/architecture.md.
 */

import { Bell, CheckCheck, Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  useAcknowledgeAllNotifications,
  useAcknowledgeNotification,
  useNotifications,
  useUnacknowledgedCount,
} from "@/hooks/use-notifications";
import { formatRelativeDate } from "@/lib/format";
import type { NotificationResponse } from "@/types/api";

const ACTION_LABELS: Record<string, string> = {
  flag_add: "Setting a flag",
  flag_remove: "Clearing a flag",
  move: "Moving a message",
  delete: "Deleting a message",
  send: "Sending a message",
  draft: "Saving a draft",
};

function NotificationRow({
  notification,
  onAcknowledge,
  isAcknowledging,
}: {
  notification: NotificationResponse;
  onAcknowledge: () => void;
  isAcknowledging: boolean;
}) {
  const detail = notification.detail ?? {};
  const subject = typeof detail.subject === "string" ? detail.subject : null;
  const stillApplied = notification.reverted_at === null;

  return (
    <div className="flex flex-col gap-1 border-b px-3 py-2 last:border-b-0">
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

export function NotificationBell({ accountId }: { accountId: string }) {
  const { data: count } = useUnacknowledgedCount(accountId);
  const { data: notifications, isLoading } = useNotifications(accountId);
  const acknowledge = useAcknowledgeNotification();
  const acknowledgeAll = useAcknowledgeAllNotifications();

  const unacknowledged = count?.unacknowledged ?? 0;

  return (
    <Popover>
      <PopoverTrigger
        render={
          <Button variant="ghost" size="icon" className="relative h-8 w-8" />
        }
        title="Notifications"
      >
        <Bell className="h-4 w-4" />
        {unacknowledged > 0 && (
          <Badge
            variant="destructive"
            className="absolute -right-1 -top-1 h-4 min-w-4 justify-center px-1 text-[10px]"
          >
            {unacknowledged > 99 ? "99+" : unacknowledged}
          </Badge>
        )}
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80 p-0">
        <div className="flex items-center justify-between border-b px-3 py-2">
          <span className="text-sm font-medium">Notifications</span>
          {unacknowledged > 0 && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 gap-1 px-2 text-xs"
              disabled={acknowledgeAll.isPending}
              onClick={() => acknowledgeAll.mutate(accountId)}
            >
              <CheckCheck className="h-3 w-3" />
              Mark all read
            </Button>
          )}
        </div>
        <div className="max-h-80 overflow-y-auto">
          {isLoading && (
            <div className="px-3 py-4 text-center text-sm text-muted-foreground">
              Loading...
            </div>
          )}
          {!isLoading && (notifications ?? []).length === 0 && (
            <div className="px-3 py-4 text-center text-sm text-muted-foreground">
              Nothing to report
            </div>
          )}
          {(notifications ?? []).map((n) => (
            <NotificationRow
              key={n.id}
              notification={n}
              isAcknowledging={
                acknowledge.isPending && acknowledge.variables?.notificationId === n.id
              }
              onAcknowledge={() =>
                acknowledge.mutate({ accountId, notificationId: n.id })
              }
            />
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}
