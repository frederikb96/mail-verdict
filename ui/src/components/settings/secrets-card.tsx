"use client";

import { useState } from "react";
import { KeyRound, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ApiError } from "@/lib/api";
import { formatRelativeDate } from "@/lib/format";
import { useDeleteSecret, usePutSecret, useSecrets } from "@/hooks/use-secrets";

/**
 * Named secrets a rule's webhook action can reference in a header, as
 * `{{secret:NAME}}`. Values are written here and never shown again; saving
 * under an existing name replaces its value.
 */
export function SecretsCard() {
  const { data: secrets, isLoading } = useSecrets();
  const putSecret = usePutSecret();
  const deleteSecret = useDeleteSecret();
  const [name, setName] = useState("");
  const [value, setValue] = useState("");

  const failure = putSecret.error ?? deleteSecret.error;
  const canSave = name.trim() !== "" && value !== "" && !putSecret.isPending;

  const save = () => {
    putSecret.mutate(
      { name: name.trim(), value },
      {
        onSuccess: () => {
          setName("");
          setValue("");
        },
      },
    );
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <KeyRound className="h-4 w-4" />
          Secrets
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="text-xs text-muted-foreground">
          Encrypted at rest and never shown again once saved. A rule&apos;s webhook action uses
          one in a header as <span className="font-mono">{"{{secret:NAME}}"}</span>.
        </p>

        {isLoading && <div className="py-2 text-sm text-muted-foreground">Loading...</div>}

        {secrets && secrets.length === 0 && (
          <div className="py-2 text-sm text-muted-foreground">No secrets stored</div>
        )}

        {secrets && secrets.length > 0 && (
          <div className="divide-y rounded-md border">
            {secrets.map((secret) => (
              <div key={secret.name} className="flex items-center justify-between px-3 py-2">
                <span className="font-mono text-sm">{secret.name}</span>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-muted-foreground">
                    {formatRelativeDate(secret.updated_at)}
                  </span>
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label={`Replace the ${secret.name} secret`}
                    onClick={() => setName(secret.name)}
                  >
                    Replace
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    aria-label={`Delete the ${secret.name} secret`}
                    disabled={deleteSecret.isPending}
                    onClick={() => deleteSecret.mutate(secret.name)}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}

        <div className="flex flex-col gap-2 sm:flex-row">
          <Input
            aria-label="Secret name"
            placeholder="NAME"
            autoComplete="off"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="font-mono sm:w-56"
          />
          <Input
            type="password"
            aria-label="Secret value"
            placeholder="Value"
            autoComplete="off"
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
          <Button size="sm" variant="outline" disabled={!canSave} onClick={save}>
            Save secret
          </Button>
        </div>

        {failure && (
          <p className="text-xs text-destructive">
            {failure instanceof ApiError ? failure.message : "The request failed"}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
