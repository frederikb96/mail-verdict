"use client";

import { useState } from "react";
import { useAtomValue } from "jotai";
import {
  Plus,
  Server,
  CheckCircle2,
  XCircle,
  RefreshCw,
  Trash2,
  Pencil,
  Plug,
  Loader2,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";

import {
  useAccounts,
  useCreateAccount,
  useDeleteAccount,
  useTestConnection,
  useUpdateAccount,
} from "@/hooks/use-accounts";
import { useStartJob, useStopJob } from "@/hooks/use-jobs";
import { syncStatesAtom } from "@/lib/atoms";
import type { AccountCreateRequest, AccountResponse } from "@/types/api";

function SyncProgressBar({
  accountId,
}: {
  accountId: string;
}) {
  const syncStates = useAtomValue(syncStatesAtom);
  const state = syncStates[accountId];

  if (!state) return null;

  const progress =
    state.synced && state.total_messages
      ? Math.round((state.synced / state.total_messages) * 100)
      : 0;

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>
          {state.current_folder ?? state.status}
          {state.folder_index !== undefined &&
            state.folder_total !== undefined &&
            ` (${state.folder_index}/${state.folder_total})`}
        </span>
        {state.synced !== undefined && state.total_messages !== undefined && (
          <span>
            {state.synced}/{state.total_messages}
          </span>
        )}
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-secondary">
        <div
          className="h-full rounded-full bg-primary transition-all duration-300"
          style={{ width: `${progress}%` }}
        />
      </div>
      {state.error_message && (
        <div className="text-xs text-destructive">{state.error_message}</div>
      )}
    </div>
  );
}

const STATE_BADGES: Record<string, { variant: "default" | "secondary" | "destructive" | "outline"; label: string }> = {
  created: { variant: "outline", label: "Created" },
  syncing: { variant: "default", label: "Syncing" },
  seeding: { variant: "default", label: "Seeding" },
  active: { variant: "secondary", label: "Active" },
  error: { variant: "destructive", label: "Error" },
};

function AccountCard({
  account,
  onEdit,
}: {
  account: AccountResponse;
  onEdit: (account: AccountResponse) => void;
}) {
  const deleteAccount = useDeleteAccount();
  const testConnection = useTestConnection();
  const startJob = useStartJob();
  const stopJob = useStopJob();
  const syncStates = useAtomValue(syncStatesAtom);
  const syncState = syncStates[account.id];

  const badgeInfo = STATE_BADGES[account.state] ?? {
    variant: "outline" as const,
    label: account.state,
  };

  const canSync = syncState?.can_sync ?? true;
  const canCancel = syncState?.can_cancel ?? false;

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-2">
            <Server className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">{account.name}</CardTitle>
          </div>
          <Badge variant={badgeInfo.variant}>{badgeInfo.label}</Badge>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-2 text-sm">
          <div className="text-muted-foreground">IMAP</div>
          <div>
            {account.imap_user}@{account.imap_host}:{account.imap_port}
          </div>
          {account.smtp_host && (
            <>
              <div className="text-muted-foreground">SMTP</div>
              <div>
                {account.smtp_user ?? account.imap_user}@{account.smtp_host}:
                {account.smtp_port}
              </div>
            </>
          )}
          <div className="text-muted-foreground">Spam</div>
          <div className="flex items-center gap-1">
            {account.spam_enabled ? (
              <CheckCircle2 className="h-3 w-3 text-green-500" />
            ) : (
              <XCircle className="h-3 w-3 text-muted-foreground" />
            )}
            {account.spam_enabled ? "Enabled" : "Disabled"}
          </div>
        </div>

        <SyncProgressBar accountId={account.id} />

        <div className="flex flex-wrap gap-2">
          {canSync && (
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                startJob.mutate({
                  name: "sync",
                  accountId: account.id,
                })
              }
            >
              <RefreshCw className="mr-1 h-3 w-3" />
              Sync
            </Button>
          )}
          {canCancel && (
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                stopJob.mutate({
                  name: "sync",
                  accountId: account.id,
                })
              }
            >
              Cancel
            </Button>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => testConnection.mutate(account.id)}
            disabled={testConnection.isPending}
          >
            {testConnection.isPending ? (
              <Loader2 className="mr-1 h-3 w-3 animate-spin" />
            ) : (
              <Plug className="mr-1 h-3 w-3" />
            )}
            Test
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onEdit(account)}
          >
            <Pencil className="mr-1 h-3 w-3" />
            Edit
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="text-destructive"
            onClick={() => {
              if (confirm(`Delete account "${account.name}"?`)) {
                deleteAccount.mutate(account.id);
              }
            }}
          >
            <Trash2 className="mr-1 h-3 w-3" />
            Delete
          </Button>
        </div>

        {testConnection.isSuccess && (
          <div className="text-sm text-green-600 dark:text-green-400">
            Connection successful
          </div>
        )}
        {testConnection.isError && (
          <div className="text-sm text-destructive">
            Connection failed: {(testConnection.error as Error).message}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function AccountForm({
  account,
  onClose,
}: {
  account?: AccountResponse;
  onClose: () => void;
}) {
  const createAccount = useCreateAccount();
  const updateAccount = useUpdateAccount();
  const isEditing = !!account;

  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const data: AccountCreateRequest = {
      name: form.get("name") as string,
      imap_host: form.get("imap_host") as string,
      imap_port: Number(form.get("imap_port")),
      imap_user: form.get("imap_user") as string,
      imap_password: (form.get("imap_password") as string) || undefined,
      smtp_host: (form.get("smtp_host") as string) || undefined,
      smtp_port: form.get("smtp_port")
        ? Number(form.get("smtp_port"))
        : undefined,
      smtp_user: (form.get("smtp_user") as string) || undefined,
      smtp_password: (form.get("smtp_password") as string) || undefined,
      sync_lookback_days: Number(form.get("sync_lookback_days")) || 180,
      spam_enabled: form.get("spam_enabled") === "on",
    };

    if (isEditing) {
      updateAccount.mutate(
        { id: account.id, data },
        { onSuccess: onClose },
      );
    } else {
      createAccount.mutate(data, { onSuccess: onClose });
    }
  };

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4">
      <div className="grid gap-3">
        <div className="grid gap-1.5">
          <Label htmlFor="name">Account Name</Label>
          <Input
            id="name"
            name="name"
            required
            defaultValue={account?.name}
            placeholder="My Email"
          />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div className="grid gap-1.5">
            <Label htmlFor="imap_host">IMAP Host</Label>
            <Input
              id="imap_host"
              name="imap_host"
              required
              defaultValue={account?.imap_host}
              placeholder="imap.example.com"
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="imap_port">IMAP Port</Label>
            <Input
              id="imap_port"
              name="imap_port"
              type="number"
              required
              defaultValue={account?.imap_port ?? 993}
            />
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div className="grid gap-1.5">
            <Label htmlFor="imap_user">IMAP User</Label>
            <Input
              id="imap_user"
              name="imap_user"
              required
              defaultValue={account?.imap_user}
              placeholder="user@example.com"
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="imap_password">IMAP Password</Label>
            <Input
              id="imap_password"
              name="imap_password"
              type="password"
              placeholder={isEditing ? "(unchanged)" : ""}
            />
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div className="grid gap-1.5">
            <Label htmlFor="smtp_host">SMTP Host</Label>
            <Input
              id="smtp_host"
              name="smtp_host"
              defaultValue={account?.smtp_host ?? ""}
              placeholder="smtp.example.com"
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="smtp_port">SMTP Port</Label>
            <Input
              id="smtp_port"
              name="smtp_port"
              type="number"
              defaultValue={account?.smtp_port ?? ""}
              placeholder="587"
            />
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div className="grid gap-1.5">
            <Label htmlFor="smtp_user">SMTP User</Label>
            <Input
              id="smtp_user"
              name="smtp_user"
              defaultValue={account?.smtp_user ?? ""}
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="smtp_password">SMTP Password</Label>
            <Input
              id="smtp_password"
              name="smtp_password"
              type="password"
              placeholder={isEditing ? "(unchanged)" : ""}
            />
          </div>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="sync_lookback_days">Sync Lookback (days)</Label>
          <Input
            id="sync_lookback_days"
            name="sync_lookback_days"
            type="number"
            defaultValue={account?.sync_lookback_days ?? 180}
          />
        </div>
        <div className="flex items-center gap-2">
          <input
            id="spam_enabled"
            name="spam_enabled"
            type="checkbox"
            defaultChecked={account?.spam_enabled ?? false}
            className="h-4 w-4"
          />
          <Label htmlFor="spam_enabled">Enable spam detection</Label>
        </div>
      </div>
      <div className="flex justify-end gap-2">
        <Button type="button" variant="outline" onClick={onClose}>
          Cancel
        </Button>
        <Button
          type="submit"
          disabled={createAccount.isPending || updateAccount.isPending}
        >
          {createAccount.isPending || updateAccount.isPending ? (
            <Loader2 className="mr-1 h-4 w-4 animate-spin" />
          ) : null}
          {isEditing ? "Update" : "Create"}
        </Button>
      </div>
    </form>
  );
}

export function AccountsPage() {
  const { data: accounts, isLoading } = useAccounts();
  const [editingAccount, setEditingAccount] = useState<
    AccountResponse | undefined
  >(undefined);
  const [dialogOpen, setDialogOpen] = useState(false);

  if (isLoading) {
    return (
      <div className="flex flex-col gap-4 p-6">
        <Skeleton className="h-8 w-48" />
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-48 w-full" />
        ))}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Accounts</h1>
        <Dialog
          open={dialogOpen}
          onOpenChange={(open) => {
            setDialogOpen(open);
            if (!open) setEditingAccount(undefined);
          }}
        >
          <DialogTrigger render={<Button />}>
            <Plus className="mr-1 h-4 w-4" />
            Add Account
          </DialogTrigger>
          <DialogContent className="max-w-lg">
            <DialogHeader>
              <DialogTitle>
                {editingAccount ? "Edit Account" : "Add Account"}
              </DialogTitle>
            </DialogHeader>
            <AccountForm
              account={editingAccount}
              onClose={() => {
                setDialogOpen(false);
                setEditingAccount(undefined);
              }}
            />
          </DialogContent>
        </Dialog>
      </div>

      {accounts?.length === 0 && (
        <div className="flex flex-col items-center gap-3 py-12 text-muted-foreground">
          <Server className="h-12 w-12 opacity-50" />
          <p>No accounts configured</p>
          <p className="text-sm">Add an email account to get started</p>
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        {accounts?.map((account) => (
          <AccountCard
            key={account.id}
            account={account}
            onEdit={(a) => {
              setEditingAccount(a);
              setDialogOpen(true);
            }}
          />
        ))}
      </div>
    </div>
  );
}
