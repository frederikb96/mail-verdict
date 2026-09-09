"use client";

/**
 * The alert bell: a durable, in-app record of something worth
 * interrupting the reader for -- currently new mail, a calendar reminder
 * in a later feature. Not account-scoped, unlike NotificationBell right
 * next to it -- an installed application watches every account from one
 * page, the same breadth alert.new itself already has.
 */

import { useRouter } from "next/navigation";
import { Bell, CheckCheck, Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  useAlerts,
  useDismissAlert,
  useDismissAllAlerts,
  useUnseenAlertCount,
} from "@/hooks/use-alerts";
import { formatRelativeDate } from "@/lib/format";
import type { AlertResponse } from "@/types/api";

function AlertRow({
  alert,
  onOpen,
  onDismiss,
  isDismissing,
}: {
  alert: AlertResponse;
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

export function AlertBell() {
  const router = useRouter();
  const { data: count } = useUnseenAlertCount();
  const { data: alerts, isLoading } = useAlerts();
  const dismiss = useDismissAlert();
  const dismissAll = useDismissAllAlerts();

  const unseen = count?.unseen ?? 0;

  const openAlert = (alert: AlertResponse) => {
    if (alert.dismissed_at === null) dismiss.mutate(alert.id);
    if (alert.url) router.push(alert.url);
  };

  return (
    <Popover>
      <PopoverTrigger render={<Button variant="ghost" size="icon" className="relative h-8 w-8" />} title="Alerts">
        <Bell className="h-4 w-4" />
        {unseen > 0 && (
          <Badge
            variant="destructive"
            className="absolute -right-1 -top-1 h-4 min-w-4 justify-center px-1 text-[10px]"
          >
            {unseen > 99 ? "99+" : unseen}
          </Badge>
        )}
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80 p-0">
        <div className="flex items-center justify-between border-b px-3 py-2">
          <span className="text-sm font-medium">Alerts</span>
          {unseen > 0 && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 gap-1 px-2 text-xs"
              disabled={dismissAll.isPending}
              onClick={() => dismissAll.mutate()}
            >
              <CheckCheck className="h-3 w-3" />
              Dismiss all
            </Button>
          )}
        </div>
        <div className="max-h-80 overflow-y-auto">
          {isLoading && (
            <div className="px-3 py-4 text-center text-sm text-muted-foreground">Loading...</div>
          )}
          {!isLoading && (alerts ?? []).length === 0 && (
            <div className="px-3 py-4 text-center text-sm text-muted-foreground">
              Nothing yet
            </div>
          )}
          {(alerts ?? []).map((alert) => (
            <AlertRow
              key={alert.id}
              alert={alert}
              onOpen={() => openAlert(alert)}
              onDismiss={() => dismiss.mutate(alert.id)}
              isDismissing={dismiss.isPending && dismiss.variables === alert.id}
            />
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}
