"use client";

/**
 * Which folders raise a system notification, and turning that on at all.
 * Permission is requested only on this explicit click -- never
 * automatically on page load, which every browser would refuse silently
 * or ignore anyway (a permission prompt not triggered by a user gesture).
 */

import { useEffect, useState } from "react";
import { useAtom } from "jotai";
import { Bell, BellOff } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { alertEnabledFolderIdsAtom } from "@/lib/alert-prefs";
import { useSearchFolders } from "@/hooks/use-search-folders";

type PermissionState = "default" | "granted" | "denied" | "unsupported";

function readPermission(): PermissionState {
  if (typeof window === "undefined" || !("Notification" in window)) return "unsupported";
  return Notification.permission;
}

export function AlertSettings() {
  const [permission, setPermission] = useState<PermissionState>("default");
  useEffect(() => setPermission(readPermission()), []);

  const [enabledFolderIds, setEnabledFolderIds] = useAtom(alertEnabledFolderIdsAtom);
  const { options, isLoading } = useSearchFolders();

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

  const requestPermission = async () => {
    if (typeof window === "undefined" || !("Notification" in window)) return;
    const result = await Notification.requestPermission();
    setPermission(result);
  };

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
          <div className="text-sm text-muted-foreground">
            {permission === "granted" && "This browser will show a system notification for new mail."}
            {permission === "denied" &&
              "Notifications are blocked for this site -- allow them in the browser's own site settings to turn this back on."}
            {permission === "default" &&
              "Turn on system notifications for new mail arriving while this tab isn't focused."}
            {permission === "unsupported" && "This browser does not support notifications."}
          </div>
          {permission === "default" && (
            <Button variant="outline" size="sm" onClick={requestPermission} className="shrink-0 gap-1.5">
              <Bell className="h-3.5 w-3.5" />
              Enable
            </Button>
          )}
          {permission === "granted" && (
            <span className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
              <Bell className="h-3.5 w-3.5" />
              Enabled
            </span>
          )}
          {(permission === "denied" || permission === "unsupported") && (
            <span className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
              <BellOff className="h-3.5 w-3.5" />
              Off
            </span>
          )}
        </div>

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
      </CardContent>
    </Card>
  );
}
