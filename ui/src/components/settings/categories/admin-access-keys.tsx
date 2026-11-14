"use client";

/**
 * Admin > Access keys: every machine client that has signed in -- an agent,
 * a script, an MCP client with its own token -- whoever it belongs to, each
 * of which an administrator can cut off.
 */

import { useMemo } from "react";

import { CategoryHeader } from "@/components/settings/blocks";
import { SignInList } from "@/components/settings/sign-in-list";
import { Skeleton } from "@/components/ui/skeleton";
import { useAdminUsers } from "@/hooks/use-admin";
import { useAdminSignIns, useRevokeSignIn } from "@/hooks/use-sign-ins";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api";

export function AdminAccessKeys() {
  const { data: keys, isLoading, error } = useAdminSignIns({ kind: "machine" });
  const { data: users } = useAdminUsers();
  const revoke = useRevokeSignIn();
  const { push } = useToast();
  const names = useMemo(
    () => new Map((users ?? []).map((user) => [user.id, user.display_name])),
    [users],
  );

  return (
    <div className="flex flex-col gap-4">
      <CategoryHeader
        title="Access keys"
        description="Agents, scripts and other clients that sign in with a token of their own. A key is a machine user at your identity provider; it appears here the first time it is used. Signing one out refuses its subject from then on."
      />
      {isLoading && <Skeleton className="h-24 w-full" />}
      {error && !keys && (
        <p role="alert" className="text-sm text-destructive">
          {error instanceof ApiError ? error.message : "Could not load the access keys."}
        </p>
      )}
      {keys && (
        <SignInList
          signIns={keys}
          isRevoking={revoke.isPending}
          ownerName={(key) => names.get(key.user_id)}
          empty="No access key has been used yet."
          onRevoke={(key) =>
            revoke.mutateAsync(key.id).catch((err: unknown) =>
              push(err instanceof ApiError ? err.message : "Could not sign it out", "error"),
            )
          }
        />
      )}
    </div>
  );
}
