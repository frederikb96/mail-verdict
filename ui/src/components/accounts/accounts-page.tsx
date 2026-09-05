"use client";

import { useState } from "react";
import { Collapsible } from "@base-ui/react/collapsible";
import {
  type LucideIcon,
  AtSign,
  Plus,
  Server,
  CheckCircle2,
  XCircle,
  RefreshCw,
  Trash2,
  Pencil,
  Loader2,
  ChevronDown,
  GripVertical,
  ImageOff,
  Layers,
  Clock,
  Zap,
  AlertCircle,
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
import { Switch } from "@/components/ui/switch";

import { FolderOrder } from "@/components/settings/folder-order";
import { ImageExceptionsList } from "@/components/settings/image-exceptions-list";
import { IdentitiesSection } from "@/components/accounts/identities-section";
import { DavAccountsSection } from "@/components/accounts/dav-account-card";
import {
  EmojiPicker,
  UnifiedNames,
} from "@/components/settings/unified-setup";

import {
  useAccounts,
  useCreateAccount,
  useDeleteAccount,
  useUpdateAccount,
} from "@/hooks/use-accounts";
import { useUpdateAccountEmoji } from "@/hooks/use-account-emoji";
import { accountConnectionState, useSyncStatus, useTriggerSync } from "@/hooks/use-sync-status";
import type {
  AccountCreateRequest,
  AccountResponse,
  AccountUpdateRequest,
} from "@/types/api";

const STATE_BADGES: Record<string, { variant: "default" | "secondary" | "destructive" | "outline"; label: string }> = {
  created: { variant: "outline", label: "Created" },
  syncing: { variant: "default", label: "Syncing" },
  disabled: { variant: "outline", label: "Disabled" },
  active: { variant: "secondary", label: "Active" },
  error: { variant: "destructive", label: "Error" },
};

/**
 * Collapsible section header with chevron indicator.
 */
function SectionTrigger({
  icon: Icon,
  label,
}: {
  icon: LucideIcon;
  label: string;
}) {
  return (
    <Collapsible.Trigger className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm font-medium hover:bg-accent [&[data-panel-open]>svg:last-child]:rotate-180">
      <Icon className="h-4 w-4 text-muted-foreground" />
      <span className="flex-1 text-left">{label}</span>
      <ChevronDown className="h-4 w-4 text-muted-foreground transition-transform duration-200" />
    </Collapsible.Trigger>
  );
}

function AccountCard({
  account,
  onEdit,
}: {
  account: AccountResponse;
  onEdit: (account: AccountResponse) => void;
}) {
  const deleteAccount = useDeleteAccount();
  const updateAccount = useUpdateAccount();
  const updateEmoji = useUpdateAccountEmoji();
  const { data: syncStatus } = useSyncStatus(account.id);
  const triggerSync = useTriggerSync();
  const [confirmDelete, setConfirmDelete] = useState(false);

  const isRetrying = accountConnectionState(account, syncStatus) === "retrying";

  const badgeInfo = isRetrying
    ? { variant: "outline" as const, label: "Retrying" }
    : (STATE_BADGES[account.state] ?? {
        variant: "outline" as const,
        label: account.state,
      });

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-2">
            <EmojiPicker
              currentEmoji={account.emoji}
              onSelect={(emoji) =>
                updateEmoji.mutate({ accountId: account.id, emoji })
              }
            />
            <CardTitle className="text-base">{account.name}</CardTitle>
          </div>
          <Badge variant={badgeInfo.variant}>{badgeInfo.label}</Badge>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {/* Sync toggle */}
        <div className="flex items-center justify-between">
          <Label className="text-sm">Sync enabled</Label>
          <Switch
            checked={account.is_active}
            onCheckedChange={(checked: boolean) =>
              updateAccount.mutate({
                id: account.id,
                data: { is_active: checked },
              })
            }
          />
        </div>

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

        {/* Sync status */}
        {syncStatus && (
          <div className="rounded-md border p-2 text-xs text-muted-foreground">
            <div className="flex items-center justify-between">
              <span className="flex items-center gap-1">
                <Zap className="h-3 w-3" />
                {syncStatus.sync_tier ?? "pending"}
              </span>
              <span className="flex items-center gap-1">
                <Clock className="h-3 w-3" />
                {syncStatus.last_incr_sync
                  ? new Date(syncStatus.last_incr_sync).toLocaleTimeString()
                  : "never"}
              </span>
            </div>
            {syncStatus.error_count > 0 && (
              <div className="mt-1 flex items-center gap-1 text-destructive">
                <AlertCircle className="h-3 w-3" />
                {syncStatus.error_count} errors — {syncStatus.last_error}
              </div>
            )}
            <div className="mt-1 text-[10px] opacity-70">
              Real-time sync via IMAP IDLE &bull; Periodic fallback every 60s
            </div>
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
            Sync
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
            {isRetrying
              ? `Reconnecting after: ${account.state_error}`
              : account.state_error}
          </div>
        )}

        <Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
          <DialogContent size="md">
            <DialogHeader>
              <DialogTitle>Delete &ldquo;{account.name}&rdquo;?</DialogTitle>
            </DialogHeader>
            <p className="text-sm text-muted-foreground">
              This removes the account and its entire locally mirrored mailbox.
              It cannot be undone. Nothing is touched on the mail server itself
              — re-adding the account re-syncs everything from scratch.
            </p>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setConfirmDelete(false)}>
                Cancel
              </Button>
              <Button
                variant="destructive"
                disabled={deleteAccount.isPending}
                onClick={() =>
                  deleteAccount.mutate(account.id, {
                    onSuccess: () => setConfirmDelete(false),
                  })
                }
              >
                {deleteAccount.isPending ? (
                  <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                ) : (
                  <Trash2 className="mr-1 h-3 w-3" />
                )}
                Delete permanently
              </Button>
            </div>
          </DialogContent>
        </Dialog>

        {/* Per-account settings sections */}
        <div className="mt-2 flex flex-col gap-1 border-t pt-3">
          <Collapsible.Root>
            <SectionTrigger icon={GripVertical} label="Folder Order & Visibility" />
            <Collapsible.Panel className="overflow-hidden">
              <div className="px-1 pt-2">
                <FolderOrder accountId={account.id} />
              </div>
            </Collapsible.Panel>
          </Collapsible.Root>

          <Collapsible.Root>
            <SectionTrigger icon={ImageOff} label="Image Exceptions" />
            <Collapsible.Panel className="overflow-hidden">
              <div className="px-1 pt-2">
                <ImageExceptionsList accountId={account.id} />
              </div>
            </Collapsible.Panel>
          </Collapsible.Root>

          <Collapsible.Root>
            <SectionTrigger icon={Layers} label="Unified View Names" />
            <Collapsible.Panel className="overflow-hidden">
              <div className="px-1 pt-2">
                <UnifiedNames accountId={account.id} />
              </div>
            </Collapsible.Panel>
          </Collapsible.Root>

          <Collapsible.Root>
            <SectionTrigger icon={AtSign} label="Sending Identities" />
            <Collapsible.Panel className="overflow-hidden">
              <div className="px-1 pt-2">
                <IdentitiesSection accountId={account.id} />
              </div>
            </Collapsible.Panel>
          </Collapsible.Root>
        </div>
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
    const name = form.get("name") as string;
    const imap_password = (form.get("imap_password") as string) || undefined;
    const smtp_host = (form.get("smtp_host") as string) || undefined;
    const smtp_port = form.get("smtp_port")
      ? Number(form.get("smtp_port"))
      : undefined;
    const smtp_user = (form.get("smtp_user") as string) || undefined;
    const smtp_password = (form.get("smtp_password") as string) || undefined;
    const spam_enabled = form.get("spam_enabled") === "on";

    if (isEditing) {
      // imap_host/imap_port/imap_user are insert-only -- not part of this payload.
      const data: AccountUpdateRequest = {
        name,
        imap_password,
        smtp_host,
        smtp_port,
        smtp_user,
        smtp_password,
        spam_enabled,
      };
      updateAccount.mutate(
        { id: account.id, data },
        { onSuccess: onClose },
      );
    } else {
      const data: AccountCreateRequest = {
        name,
        imap_host: form.get("imap_host") as string,
        imap_port: Number(form.get("imap_port")),
        imap_user: form.get("imap_user") as string,
        imap_password,
        smtp_host,
        smtp_port,
        smtp_user,
        smtp_password,
        spam_enabled,
      };
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
        {isEditing && (
          <p className="text-xs text-muted-foreground">
            IMAP host, port and user can&apos;t be changed on an existing
            account — delete and re-add it to connect to a different server.
          </p>
        )}
        <div className="grid grid-cols-2 gap-3">
          <div className="grid gap-1.5">
            <Label htmlFor="imap_host">IMAP Host</Label>
            <Input
              id="imap_host"
              name="imap_host"
              required={!isEditing}
              disabled={isEditing}
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
              required={!isEditing}
              disabled={isEditing}
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
              required={!isEditing}
              disabled={isEditing}
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
          <DialogContent size="lg">
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

      <div className="flex flex-col gap-4">
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

      <DavAccountsSection />
    </div>
  );
}
