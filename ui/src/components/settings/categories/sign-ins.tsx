"use client";

/**
 * Settings > Signed-in devices: the caller's own phones, browsers and
 * machine clients, each of which they can sign out.
 */

import { CategoryHeader } from "@/components/settings/blocks";
import { SignInList } from "@/components/settings/sign-in-list";
import { Skeleton } from "@/components/ui/skeleton";
import { useOwnSignIns, useRevokeOwnSignIn } from "@/hooks/use-sign-ins";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api";

export function SignInsSettings() {
  const { data: signIns, isLoading, error } = useOwnSignIns();
  const revoke = useRevokeOwnSignIn();
  const { push } = useToast();

  return (
    <div className="flex flex-col gap-4">
      <CategoryHeader
        title="Signed-in devices"
        description="Every phone and access key that has used your account, and your web browsers together. Signing one out takes effect on its next request."
      />
      {isLoading && <Skeleton className="h-24 w-full" />}
      {error && !signIns && (
        <p role="alert" className="text-sm text-destructive">
          {error instanceof ApiError ? error.message : "Could not load your sign-ins."}
        </p>
      )}
      {signIns && (
        <SignInList
          signIns={signIns}
          isRevoking={revoke.isPending}
          empty="Nothing has signed in yet."
          onRevoke={(signIn) =>
            revoke.mutateAsync(signIn.id).catch((err: unknown) =>
              push(err instanceof ApiError ? err.message : "Could not sign it out", "error"),
            )
          }
        />
      )}
    </div>
  );
}
