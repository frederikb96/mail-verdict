"use client";

/**
 * A list of sign-ins -- phones, a person's browsers, machine clients --
 * with a confirmed revoke per row. Used for one's own sign-ins in Settings
 * and for anyone's in Administration; the caller supplies the revoke.
 */

import { useState } from "react";
import { Globe, KeyRound, Smartphone } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { formatRelativeAgo, formatRelativeDate } from "@/lib/format";
import { revokeConsequence, signInLabel } from "@/lib/sign-ins";
import type { SignIn } from "@/types/api";

const KIND_ICON = { phone: Smartphone, browser: Globe, machine: KeyRound } as const;

export function SignInList({
  signIns,
  onRevoke,
  isRevoking,
  ownerName,
  empty,
}: {
  signIns: SignIn[];
  /** Resolves once the revoke has been answered. */
  onRevoke: (signIn: SignIn) => Promise<unknown>;
  isRevoking: boolean;
  /** Who a row belongs to, for a list spanning several people. */
  ownerName?: (signIn: SignIn) => string | undefined;
  empty: string;
}) {
  const [revoking, setRevoking] = useState<SignIn | null>(null);

  if (signIns.length === 0) return <p className="text-sm text-muted-foreground">{empty}</p>;

  return (
    <>
      <div className="flex flex-col gap-2">
        {signIns.map((signIn) => {
          const Icon = KIND_ICON[signIn.kind];
          const label = signInLabel(signIn);
          const owner = ownerName?.(signIn);
          const revoked = signIn.revoked_at !== null;
          return (
            <div
              key={signIn.id}
              data-testid="sign-in"
              className="flex items-center gap-2 rounded-lg border px-3 py-2 text-sm"
            >
              <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className={revoked ? "break-all text-muted-foreground line-through" : "break-all"}>
                    {label}
                  </span>
                  {signIn.current && <Badge variant="outline">this one</Badge>}
                  {revoked && <Badge variant="destructive">signed out</Badge>}
                </div>
                <div className="text-xs text-muted-foreground">
                  {owner && <>{owner} · </>}
                  {revoked
                    ? `signed out ${formatRelativeAgo(signIn.revoked_at)}`
                    : `last used ${formatRelativeAgo(signIn.last_seen_at)}`}
                  {" · first "}
                  {formatRelativeDate(signIn.first_seen_at)}
                </div>
              </div>
              {!revoked && (
                <Button
                  variant="outline"
                  size="sm"
                  className="shrink-0"
                  aria-label={`Sign out ${label}`}
                  onClick={() => setRevoking(signIn)}
                >
                  Sign out
                </Button>
              )}
            </div>
          );
        })}
      </div>
      <ConfirmDialog
        open={revoking !== null}
        onOpenChange={(open) => !open && setRevoking(null)}
        title={revoking ? `Sign out ${signInLabel(revoking)}?` : ""}
        description={
          revoking
            ? `${revokeConsequence(revoking)}${revoking.current ? " That includes this one, right away." : ""}`
            : ""
        }
        confirmLabel="Sign out"
        isConfirming={isRevoking}
        onConfirm={() => {
          if (!revoking) return;
          void onRevoke(revoking).finally(() => setRevoking(null));
        }}
      />
    </>
  );
}
