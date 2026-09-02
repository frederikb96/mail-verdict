"use client";

/** Named sending addresses on a mail account -- list, add, edit, delete,
 * pick a default. Rendered inside each AccountCard's collapsible sections. */

import { useState } from "react";
import { Loader2, Plus, Star, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  useCreateIdentity,
  useDeleteIdentity,
  useIdentities,
  useUpdateIdentity,
} from "@/hooks/use-identities";

export function IdentitiesSection({ accountId }: { accountId: string }) {
  const { data: identities, isLoading } = useIdentities(accountId);
  const createIdentity = useCreateIdentity();
  const updateIdentity = useUpdateIdentity();
  const deleteIdentity = useDeleteIdentity();
  const [address, setAddress] = useState("");
  const [displayName, setDisplayName] = useState("");

  if (isLoading) {
    return <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />;
  }

  return (
    <div className="flex flex-col gap-2">
      {(identities ?? []).map((identity) => (
        <div key={identity.id} className="flex items-center gap-2 rounded-md border px-2 py-1.5 text-sm">
          <Button
            variant="ghost"
            size="icon-xs"
            title={identity.is_default ? "Default identity" : "Set as default"}
            onClick={() => updateIdentity.mutate({ id: identity.id, data: { is_default: true } })}
          >
            <Star className={identity.is_default ? "h-3.5 w-3.5 fill-yellow-400 text-yellow-400" : "h-3.5 w-3.5"} />
          </Button>
          <div className="flex min-w-0 flex-1 flex-col">
            <span className="truncate">{identity.display_name || identity.address}</span>
            {identity.display_name && (
              <span className="truncate text-xs text-muted-foreground">{identity.address}</span>
            )}
          </div>
          <Button
            variant="ghost"
            size="icon-xs"
            className="text-destructive"
            onClick={() => deleteIdentity.mutate(identity.id)}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      ))}
      {(identities ?? []).length === 0 && (
        <p className="text-xs text-muted-foreground">No additional identities</p>
      )}

      <form
        className="flex items-center gap-1.5"
        onSubmit={(e) => {
          e.preventDefault();
          if (!address.trim()) return;
          createIdentity.mutate(
            {
              account_id: accountId,
              address: address.trim(),
              display_name: displayName.trim() || undefined,
              is_default: (identities ?? []).length === 0,
            },
            {
              onSuccess: () => {
                setAddress("");
                setDisplayName("");
              },
            },
          );
        }}
      >
        <Input
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder="Name"
          className="h-7 w-28"
        />
        <Input
          value={address}
          onChange={(e) => setAddress(e.target.value)}
          placeholder="address@example.com"
          className="h-7 flex-1"
        />
        <Button type="submit" size="icon-sm" disabled={createIdentity.isPending}>
          {createIdentity.isPending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Plus className="h-3.5 w-3.5" />
          )}
        </Button>
      </form>
    </div>
  );
}
