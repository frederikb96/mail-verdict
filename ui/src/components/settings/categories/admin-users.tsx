"use client";

/**
 * Admin > Users: who is on this instance, their role, and which accounts
 * they own, by name. An administrator manages people and ownership here and
 * never sees what is inside anyone's accounts.
 */

import { useMemo, useState } from "react";
import { ChevronDown, MoreVertical, UserPlus } from "lucide-react";

import { CategoryHeader } from "@/components/settings/blocks";
import { OptionSelect } from "@/components/settings/setting-fields";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  useAdminAccounts,
  useAdminDavAccounts,
  useAdminUsers,
  useCreateAdminUser,
  useSetAccountOwner,
  useUpdateAdminUser,
} from "@/hooks/use-admin";
import { useMe } from "@/hooks/use-me";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api";
import { formatRelativeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { AdminAccount, AdminUser } from "@/types/api";

type KindedAccount = AdminAccount & { kind: "mail" | "dav" };

const UNOWNED = "unowned";

function errorText(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

function StateDot({ state }: { state: string }) {
  const bad = state === "error";
  return (
    <span
      role="img"
      aria-label={bad ? "Has an error" : state}
      className={cn("inline-block h-2 w-2 shrink-0 rounded-full", bad ? "bg-destructive" : "bg-emerald-500")}
    />
  );
}

/** "Posteo, RWTH, +2" with a red dot when any of them is in error. */
function AccountSummary({ accounts }: { accounts: KindedAccount[] }) {
  if (accounts.length === 0) return <span>No accounts</span>;
  const shown = accounts.slice(0, 3).map((a) => a.name);
  const rest = accounts.length - shown.length;
  return (
    <span className="inline-flex items-center gap-1.5">
      {accounts.some((a) => a.state === "error") && <StateDot state="error" />}
      <span className="break-words">
        {shown.join(", ")}
        {rest > 0 ? `, +${rest}` : ""}
      </span>
    </span>
  );
}

// --- Dialogs ------------------------------------------------------------

function PreCreateDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const create = useCreateAdminUser();
  const [subject, setSubject] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<"user" | "admin">("user");
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setSubject("");
    setDisplayName("");
    setEmail("");
    setRole("user");
    setError(null);
  };

  const submit = () => {
    if (create.isPending) return;
    if (!subject.trim() || !displayName.trim()) {
      setError("A sign-in subject and a name are required.");
      return;
    }
    create.mutate(
      {
        subject: subject.trim(),
        display_name: displayName.trim(),
        email: email.trim() || null,
        role,
      },
      {
        onSuccess: () => {
          reset();
          onOpenChange(false);
        },
        onError: (err) => setError(errorText(err, "Could not add the user")),
      },
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>Pre-create a user</DialogTitle>
        </DialogHeader>
        <p className="text-sm text-muted-foreground">
          For someone who has not signed in yet, such as a test user. Everyone else appears here on
          their first sign-in.
        </p>
        <div className="grid gap-3">
          <div className="grid gap-1.5">
            <Label htmlFor="new-user-subject">Sign-in subject</Label>
            <Input
              id="new-user-subject"
              value={subject}
              placeholder="The sub claim their sign-in carries"
              onChange={(e) => setSubject(e.target.value)}
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="new-user-name">Name</Label>
            <Input
              id="new-user-name"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="new-user-email">Email (optional)</Label>
            <Input id="new-user-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
          </div>
          <div className="grid gap-1.5">
            <Label>Role</Label>
            <OptionSelect
              aria-label="Role"
              value={role}
              options={[
                { value: "user", label: "User" },
                { value: "admin", label: "Admin" },
              ]}
              className="w-40"
              onChange={(v) => setRole(v as "user" | "admin")}
            />
          </div>
        </div>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={create.isPending} onClick={submit}>
            Add user
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function RenameDialog({ user, onClose }: { user: AdminUser; onClose: () => void }) {
  const update = useUpdateAdminUser();
  const [name, setName] = useState(user.display_name);
  const [error, setError] = useState<string | null>(null);
  const submit = () => {
    if (!name.trim() || update.isPending) return;
    update.mutate(
      { id: user.id, data: { display_name: name.trim() } },
      { onSuccess: onClose, onError: (err) => setError(errorText(err, "Could not rename")) },
    );
  };
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent size="sm">
        <DialogHeader>
          <DialogTitle>Rename user</DialogTitle>
        </DialogHeader>
        <div className="grid gap-1.5">
          <Label htmlFor="rename-user">Name</Label>
          <Input
            id="rename-user"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
          />
        </div>
        {error && <p className="text-sm text-destructive">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={!name.trim() || update.isPending} onClick={submit}>
            Save
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** Move one account to another user. Opened from an account row (account
 * known) or from a user's menu (pick one of their accounts first). */
function ReassignDialog({
  accounts,
  account,
  users,
  onClose,
}: {
  /** The accounts to choose from when none is preselected. */
  accounts: KindedAccount[];
  account: KindedAccount | null;
  users: AdminUser[];
  onClose: () => void;
}) {
  const setOwner = useSetAccountOwner();
  const { push } = useToast();
  const [pickedId, setPickedId] = useState<string>(account?.id ?? accounts[0]?.id ?? "");
  const chosen = account ?? accounts.find((a) => a.id === pickedId) ?? null;
  const owner = users.find((u) => u.id === chosen?.owner_user_id);
  const targets = users.filter((u) => u.id !== chosen?.owner_user_id);
  const [targetId, setTargetId] = useState<string>("");
  const target = targets.find((u) => u.id === targetId);
  const [error, setError] = useState<string | null>(null);

  const submit = () => {
    if (!chosen || !target || setOwner.isPending) return;
    setOwner.mutate(
      { kind: chosen.kind, accountId: chosen.id, userId: target.id },
      {
        onSuccess: () => {
          push(`“${chosen.name}” now belongs to ${target.display_name}`, "success");
          onClose();
        },
        onError: (err) => setError(errorText(err, "Could not move the account")),
      },
    );
  };

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>Reassign an account</DialogTitle>
        </DialogHeader>
        <div className="grid gap-3">
          {!account && (
            <div className="grid gap-1.5">
              <Label>Account</Label>
              <OptionSelect
                aria-label="Account"
                value={pickedId}
                options={accounts.map((a) => ({
                  value: a.id,
                  label: `${a.name}${a.kind === "dav" ? " (calendar and contacts)" : ""}`,
                }))}
                className="w-full"
                onChange={(v) => setPickedId(String(v))}
              />
            </div>
          )}
          <div className="grid gap-1.5">
            <Label>Give it to</Label>
            <OptionSelect
              aria-label="New owner"
              value={targetId}
              options={targets.map((u) => ({ value: u.id, label: u.display_name }))}
              placeholder="Choose a user"
              className="w-full"
              onChange={(v) => setTargetId(String(v))}
            />
          </div>
        </div>
        {chosen && target && (
          <p className="text-sm text-muted-foreground">
            “{chosen.name}” moves from {owner ? owner.display_name : "nobody"} to{" "}
            {target.display_name}: they see it in their accounts and the previous owner no longer
            does. Nothing changes on the mail server.
          </p>
        )}
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={!chosen || !target || setOwner.isPending} onClick={submit}>
            Reassign
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// --- Rows ---------------------------------------------------------------

function AccountLine({
  account,
  onReassign,
}: {
  account: KindedAccount;
  onReassign: () => void;
}) {
  return (
    <div className="flex items-center gap-2 py-1.5">
      <StateDot state={account.state} />
      <span className="min-w-0 flex-1 break-words text-sm">
        {account.name}
        {account.kind === "dav" && (
          <span className="ml-1.5 text-xs text-muted-foreground">calendar and contacts</span>
        )}
      </span>
      {!account.is_active && <Badge variant="outline">off</Badge>}
      <DropdownMenu>
        <DropdownMenuTrigger
          render={<Button variant="ghost" size="icon-sm" aria-label={`Actions for ${account.name}`} />}
        >
          <MoreVertical className="h-4 w-4" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onClick={onReassign}>Reassign to…</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

type Pending =
  | { kind: "role"; user: AdminUser }
  | { kind: "disable"; user: AdminUser };

function UserRow({
  user,
  accounts,
  isSelf,
  onRename,
  onReassign,
  onPending,
  onEnable,
}: {
  user: AdminUser;
  accounts: KindedAccount[];
  isSelf: boolean;
  onRename: () => void;
  onReassign: (account: KindedAccount | null) => void;
  onPending: (p: Pending) => void;
  onEnable: () => void;
}) {
  const [open, setOpen] = useState(false);
  const isAdmin = user.role === "admin";
  const locked = isSelf || user.is_owner;
  return (
    <div data-testid="user-row" className="border-b last:border-b-0">
      <div className="flex items-start gap-2 px-3 py-3">
        <button
          type="button"
          aria-expanded={open}
          aria-label={`${open ? "Hide" : "Show"} accounts of ${user.display_name}`}
          className="mt-0.5 rounded p-0.5 text-muted-foreground hover:text-foreground"
          onClick={() => setOpen((v) => !v)}
        >
          <ChevronDown className={cn("h-4 w-4 transition-transform", !open && "-rotate-90")} />
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="break-words text-sm font-medium">{user.display_name}</span>
            {isSelf && <Badge variant="outline">you</Badge>}
            <Badge variant={isAdmin ? "default" : "secondary"}>{isAdmin ? "Admin" : "User"}</Badge>
            {user.disabled && <Badge variant="destructive">sign-in disabled</Badge>}
            {user.is_owner && <Badge variant="outline">instance owner</Badge>}
            {!user.last_seen_at && !locked && <Badge variant="outline">not signed in yet</Badge>}
          </div>
          <div className="mt-1 flex flex-col gap-0.5 text-xs text-muted-foreground">
            {(user.email || !user.is_owner) && (
              <span className="break-all">{user.email ?? "machine user"}</span>
            )}
            <span>
              <AccountSummary accounts={accounts} />
            </span>
            <span>
              {user.account_count} mail · {user.dav_account_count} calendar · last seen{" "}
              {isSelf ? "now" : user.last_seen_at ? formatRelativeAgo(user.last_seen_at) : "never"}
            </span>
          </div>
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <Button variant="ghost" size="icon" className="h-8 w-8" aria-label={`Actions for ${user.display_name}`} />
            }
          >
            <MoreVertical className="h-4 w-4" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-48">
            {!locked && (
              <DropdownMenuItem onClick={() => onPending({ kind: "role", user })}>
                {isAdmin ? "Remove admin" : "Make admin"}
              </DropdownMenuItem>
            )}
            <DropdownMenuItem onClick={onRename}>Rename…</DropdownMenuItem>
            <DropdownMenuItem disabled={accounts.length === 0} onClick={() => onReassign(null)}>
              Reassign an account…
            </DropdownMenuItem>
            {!locked && (
              <>
                <DropdownMenuSeparator />
                {user.disabled ? (
                  <DropdownMenuItem onClick={onEnable}>Enable sign-in</DropdownMenuItem>
                ) : (
                  <DropdownMenuItem variant="destructive" onClick={() => onPending({ kind: "disable", user })}>
                    Disable sign-in
                  </DropdownMenuItem>
                )}
              </>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {open && (
        <div className="border-t bg-muted/20 px-4 py-2 pl-10">
          {accounts.length === 0 && <p className="py-1 text-xs text-muted-foreground">No accounts.</p>}
          {accounts.map((a) => (
            <AccountLine key={`${a.kind}-${a.id}`} account={a} onReassign={() => onReassign(a)} />
          ))}
        </div>
      )}
    </div>
  );
}

export function AdminUsers() {
  const me = useMe();
  const { push } = useToast();
  const { data: users, isLoading, error } = useAdminUsers();
  const { data: mailAccounts } = useAdminAccounts();
  const { data: davAccounts } = useAdminDavAccounts();
  const update = useUpdateAdminUser();

  const [createOpen, setCreateOpen] = useState(false);
  const [renaming, setRenaming] = useState<AdminUser | null>(null);
  const [reassign, setReassign] = useState<{ owner: string; account: KindedAccount | null } | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);

  const byOwner = useMemo(() => {
    const map = new Map<string, KindedAccount[]>();
    const all: KindedAccount[] = [
      ...(mailAccounts ?? []).map((a) => ({ ...a, kind: "mail" as const })),
      ...(davAccounts ?? []).map((a) => ({ ...a, kind: "dav" as const })),
    ];
    for (const account of all) {
      const key = account.owner_user_id ?? UNOWNED;
      map.set(key, [...(map.get(key) ?? []), account]);
    }
    return map;
  }, [mailAccounts, davAccounts]);

  const unowned = byOwner.get(UNOWNED) ?? [];

  const mutate = (user: AdminUser, data: { role?: "admin" | "user"; disabled?: boolean }, done: () => void) =>
    update.mutate(
      { id: user.id, data },
      {
        onSuccess: done,
        onError: (err) => {
          done();
          push(errorText(err, "Could not save the change"), "error");
        },
      },
    );

  return (
    <div className="flex flex-col gap-4">
      <CategoryHeader
        title="Users"
        description="Sign-in is through your identity provider; a person appears here after their first sign-in. You manage who exists and who owns which account, and you never see anyone's mail."
        actions={
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <UserPlus className="mr-1 h-3.5 w-3.5" />
            Pre-create user…
          </Button>
        }
      />

      {isLoading && (
        <div className="flex flex-col gap-2">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-16 w-full" />
          ))}
        </div>
      )}
      {error && !users && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(error, "Could not load the users.")}
        </p>
      )}
      {users && users.length <= 1 && (
        <p className="text-sm text-muted-foreground">Only you so far.</p>
      )}
      {users && (
        <div className="overflow-hidden rounded-xl bg-card ring-1 ring-foreground/10">
          {users.map((user) => (
            <UserRow
              key={user.id}
              user={user}
              isSelf={user.id === me.user_id}
              accounts={byOwner.get(user.id) ?? []}
              onRename={() => setRenaming(user)}
              onReassign={(account) => setReassign({ owner: user.id, account })}
              onPending={setPending}
              onEnable={() => mutate(user, { disabled: false }, () => undefined)}
            />
          ))}
        </div>
      )}

      {unowned.length > 0 && (
        <div className="rounded-xl bg-card px-4 py-3 ring-1 ring-foreground/10">
          <h2 className="text-sm font-medium">Unowned accounts</h2>
          <p className="mb-1 text-xs text-muted-foreground">
            Nobody can see these until they are given to someone.
          </p>
          {unowned.map((a) => (
            <AccountLine
              key={`${a.kind}-${a.id}`}
              account={a}
              onReassign={() => setReassign({ owner: UNOWNED, account: a })}
            />
          ))}
        </div>
      )}

      <PreCreateDialog open={createOpen} onOpenChange={setCreateOpen} />
      {renaming && <RenameDialog user={renaming} onClose={() => setRenaming(null)} />}
      {reassign && users && (
        <ReassignDialog
          users={users}
          account={reassign.account}
          accounts={byOwner.get(reassign.owner) ?? []}
          onClose={() => setReassign(null)}
        />
      )}
      {pending?.kind === "role" && (
        <ConfirmDialog
          open
          onOpenChange={(open) => !open && setPending(null)}
          title={
            pending.user.role === "admin"
              ? `Remove admin from ${pending.user.display_name}?`
              : `Make ${pending.user.display_name} an admin?`
          }
          description={
            pending.user.role === "admin"
              ? "They keep their own mail and rules but lose access to Administration."
              : "They gain access to Administration: models, provider keys, queues and other people's accounts by name. They still cannot read anyone's mail."
          }
          confirmLabel={pending.user.role === "admin" ? "Remove admin" : "Make admin"}
          confirmVariant="default"
          isConfirming={update.isPending}
          onConfirm={() =>
            mutate(pending.user, { role: pending.user.role === "admin" ? "user" : "admin" }, () =>
              setPending(null),
            )
          }
        />
      )}
      {pending?.kind === "disable" && (
        <ConfirmDialog
          open
          onOpenChange={(open) => !open && setPending(null)}
          title={`Disable sign-in for ${pending.user.display_name}?`}
          description="They can no longer use MailVerdict until you enable them again. Their accounts and rules stay as they are."
          confirmLabel="Disable sign-in"
          isConfirming={update.isPending}
          onConfirm={() => mutate(pending.user, { disabled: true }, () => setPending(null))}
        />
      )}
    </div>
  );
}
