"use client";

/** CalDAV/CardDAV account under "Calendar and contact servers" -- the DAV
 * analogue of AccountCard, using the same never-connected vs retrying split
 * mail accounts use, since PostIMAP's `state`/`state_error` and this
 * backend's DAV sync state carry the same shape. */

import { useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  Plus,
  RefreshCw,
  Server,
  Trash2,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  useCreateDavAccount,
  useDavAccounts,
  useDeleteDavAccount,
  useTriggerDavSync,
  useUpdateDavAccount,
} from "@/hooks/use-dav-accounts";
import { formatRelativeDate } from "@/lib/format";
import type { DavAccountResponse } from "@/types/api";

const STATE_BADGES: Record<string, { variant: "default" | "secondary" | "destructive" | "outline"; label: string }> = {
  created: { variant: "outline", label: "Created" },
  syncing: { variant: "default", label: "Syncing" },
  active: { variant: "secondary", label: "Active" },
  error: { variant: "destructive", label: "Error" },
  disabled: { variant: "outline", label: "Disabled" },
};

function DavAccountForm({ onClose }: { onClose: () => void }) {
  const createDavAccount = useCreateDavAccount();

  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        const form = new FormData(e.currentTarget);
        createDavAccount.mutate(
          {
            name: form.get("name") as string,
            discovery_url: form.get("discovery_url") as string,
            username: form.get("username") as string,
            password: form.get("password") as string,
          },
          { onSuccess: onClose },
        );
      }}
    >
      <div className="grid gap-1.5">
        <Label htmlFor="dav-name">Name</Label>
        <Input id="dav-name" name="name" required placeholder="Nextcloud" />
      </div>
      <div className="grid gap-1.5">
        <Label htmlFor="dav-url">Server URL</Label>
        <Input id="dav-url" name="discovery_url" required placeholder="https://cloud.example.com" />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="grid gap-1.5">
          <Label htmlFor="dav-user">Username</Label>
          <Input id="dav-user" name="username" required />
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="dav-password">App password</Label>
          <Input id="dav-password" name="password" type="password" required />
        </div>
      </div>
      <p className="text-xs text-muted-foreground">
        Use an app-specific password where the server supports one, rather than the account
        password itself.
      </p>
      <div className="flex justify-end gap-2">
        <Button type="button" variant="outline" onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" disabled={createDavAccount.isPending}>
          {createDavAccount.isPending && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}
          Add
        </Button>
      </div>
    </form>
  );
}

function DavAccountCard({ account }: { account: DavAccountResponse }) {
  const updateDavAccount = useUpdateDavAccount();
  const deleteDavAccount = useDeleteDavAccount();
  const triggerSync = useTriggerDavSync();
  const [confirmDelete, setConfirmDelete] = useState(false);

  const hasSyncedBefore = account.collections.some((c) => c.initial_sync_done);
  const isRetrying = account.state === "error" && hasSyncedBefore;
  const badgeInfo = isRetrying
    ? { variant: "outline" as const, label: "Retrying" }
    : STATE_BADGES[account.state] ?? { variant: "outline" as const, label: account.state };

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between">
          <CardTitle className="text-base">{account.name}</CardTitle>
          <Badge variant={badgeInfo.variant}>{badgeInfo.label}</Badge>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <Label className="text-sm">Sync enabled</Label>
          <Switch
            checked={account.is_active}
            onCheckedChange={(checked: boolean) =>
              updateDavAccount.mutate({ id: account.id, data: { is_active: checked } })
            }
          />
        </div>

        <div className="grid grid-cols-2 gap-2 text-sm">
          <div className="text-muted-foreground">Server</div>
          <div className="truncate">{account.discovery_url}</div>
          <div className="text-muted-foreground">Synced</div>
          <div>
            {account.last_polled_at ? formatRelativeDate(account.last_polled_at) : "never"}
          </div>
        </div>

        {account.collections.length > 0 && (
          <div className="flex flex-col gap-1 rounded-md border p-2 text-xs">
            {account.collections.map((c) => (
              <div key={c.id} className="flex items-center justify-between">
                <span className="flex items-center gap-1">
                  {c.initial_sync_done ? (
                    <CheckCircle2 className="h-3 w-3 text-green-500" />
                  ) : (
                    <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />
                  )}
                  {c.display_name ?? c.kind}
                </span>
                <span className="text-muted-foreground">
                  {c.initial_sync_done
                    ? `${c.total_count} items`
                    : c.backfill_total
                      ? `${c.total_count}/${c.backfill_total}`
                      : "syncing…"}
                </span>
              </div>
            ))}
          </div>
        )}

        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => triggerSync.mutate(account.id)}
            disabled={triggerSync.isPending}
          >
            {triggerSync.isPending ? (
              <Loader2 className="mr-1 h-3 w-3 animate-spin" />
            ) : (
              <RefreshCw className="mr-1 h-3 w-3" />
            )}
            Sync now
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="text-destructive"
            onClick={() => setConfirmDelete(true)}
          >
            <Trash2 className="mr-1 h-3 w-3" />
            Delete
          </Button>
        </div>

        {account.state === "error" && account.state_error && (
          <div
            className={
              isRetrying
                ? "flex items-center gap-1 text-sm text-muted-foreground"
                : "flex items-center gap-1 text-sm text-destructive"
            }
          >
            <AlertCircle className="h-3.5 w-3.5 shrink-0" />
            {isRetrying ? `Reconnecting after: ${account.state_error}` : account.state_error}
          </div>
        )}
      </CardContent>

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={`Delete "${account.name}"?`}
        description="This removes the server and its mirrored calendars and contacts. It cannot be undone."
        isConfirming={deleteDavAccount.isPending}
        onConfirm={() =>
          deleteDavAccount.mutate(account.id, { onSuccess: () => setConfirmDelete(false) })
        }
      />
    </Card>
  );
}

export function DavAccountsSection() {
  const { data: davAccounts, isLoading } = useDavAccounts();
  const [dialogOpen, setDialogOpen] = useState(false);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Calendar and contact servers</h2>
        <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
          <DialogTrigger render={<Button variant="outline" />}>
            <Plus className="mr-1 h-4 w-4" />
            Add server
          </DialogTrigger>
          <DialogContent className="max-w-lg">
            <DialogHeader>
              <DialogTitle>Add a CalDAV/CardDAV server</DialogTitle>
            </DialogHeader>
            <DavAccountForm onClose={() => setDialogOpen(false)} />
          </DialogContent>
        </Dialog>
      </div>

      {isLoading && <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />}

      {!isLoading && (davAccounts ?? []).length === 0 && (
        <div className="flex flex-col items-center gap-2 py-8 text-muted-foreground">
          <Server className="h-10 w-10 opacity-50" />
          <p className="text-sm">No calendar or contact servers configured</p>
        </div>
      )}

      <div className="flex flex-col gap-4">
        {davAccounts?.map((account) => (
          <DavAccountCard key={account.id} account={account} />
        ))}
      </div>
    </div>
  );
}

export { DavAccountCard };
