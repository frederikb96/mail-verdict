"use client";

import { useState } from "react";
import { Loader2, Radio, AlertCircle, CheckCircle2 } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  useIdleFolders,
  useToggleIdle,
  useValidateIdle,
} from "@/hooks/use-idle-config";

interface IdleConfigProps {
  accountId: string | null;
}

/**
 * IMAP IDLE per-folder configuration.
 * Checkboxes with immediate validation on toggle.
 */
export function IdleConfig({ accountId }: IdleConfigProps) {
  const { data: idleFolders, isLoading } = useIdleFolders(accountId);
  const toggleIdle = useToggleIdle();
  const validateIdle = useValidateIdle();
  const [validating, setValidating] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});

  if (!accountId) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <Radio className="h-4 w-4" />
            IMAP IDLE
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Select an account to configure IDLE streams
          </p>
        </CardContent>
      </Card>
    );
  }

  const handleToggle = async (folderId: string, currentEnabled: boolean) => {
    const newEnabled = !currentEnabled;

    if (newEnabled) {
      // Validate first before enabling
      setValidating(folderId);
      setErrors((prev) => {
        const next = { ...prev };
        delete next[folderId];
        return next;
      });

      try {
        const result = await validateIdle.mutateAsync({
          accountId,
          folderId,
        });
        if (!result.supported) {
          setErrors((prev) => ({
            ...prev,
            [folderId]: result.error ?? "IDLE not supported",
          }));
          setValidating(null);
          return;
        }
      } catch {
        setErrors((prev) => ({
          ...prev,
          [folderId]: "Validation failed",
        }));
        setValidating(null);
        return;
      }
    }

    toggleIdle.mutate(
      { accountId, folderId, enabled: newEnabled },
      {
        onSettled: () => setValidating(null),
      },
    );
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <Radio className="h-4 w-4" />
          IMAP IDLE
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="mb-3 text-xs text-muted-foreground">
          Enable real-time push notifications per folder. IDLE support is
          validated when toggling on.
        </p>

        {isLoading && (
          <div className="py-4 text-sm text-muted-foreground">Loading...</div>
        )}

        {!isLoading && (!idleFolders || idleFolders.length === 0) && (
          <div className="py-4 text-sm text-muted-foreground">
            No folders available
          </div>
        )}

        {idleFolders && idleFolders.length > 0 && (
          <div className="divide-y rounded-md border">
            {idleFolders.map((folder) => (
              <div key={folder.folder_id} className="px-3 py-2">
                <div className="flex items-center justify-between">
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={folder.idle_enabled}
                      disabled={validating === folder.folder_id}
                      onChange={() =>
                        handleToggle(folder.folder_id, folder.idle_enabled)
                      }
                      className="h-4 w-4"
                    />
                    {folder.imap_name}
                  </label>
                  {validating === folder.folder_id && (
                    <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
                  )}
                  {folder.idle_enabled && !errors[folder.folder_id] && (
                    <CheckCircle2 className="h-3.5 w-3.5 text-green-500" />
                  )}
                </div>
                {errors[folder.folder_id] && (
                  <div className="mt-1 flex items-center gap-1 text-xs text-destructive">
                    <AlertCircle className="h-3 w-3" />
                    {errors[folder.folder_id]}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
