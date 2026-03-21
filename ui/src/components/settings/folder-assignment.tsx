"use client";

import { useState, useEffect } from "react";
import { Save, Loader2, RefreshCw, FolderInput } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useFolders } from "@/hooks/use-folders";
import {
  useFolderMapping,
  useAutoDetectMapping,
  useUpdateFolderMapping,
} from "@/hooks/use-folder-assignment";

const ROLE_LABELS: Record<string, string> = {
  inbox: "Inbox",
  spam: "Spam",
  drafts: "Drafts",
  sent: "Sent",
  archive: "Archive",
  trash: "Trash",
};

const ROLES = Object.keys(ROLE_LABELS);

interface FolderAssignmentProps {
  accountId: string | null;
}

/**
 * Folder role assignment dialog: map IMAP folders to inbox/spam/drafts/sent/archive/trash.
 */
export function FolderAssignment({ accountId }: FolderAssignmentProps) {
  const { data: folders } = useFolders(accountId);
  const { data: currentMapping } = useFolderMapping(accountId);
  const autoDetect = useAutoDetectMapping();
  const updateMapping = useUpdateFolderMapping();

  const [mapping, setMapping] = useState<Record<string, string | null>>({});
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (currentMapping) {
      setMapping(currentMapping);
      setDirty(false);
    }
  }, [currentMapping]);

  if (!accountId) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <FolderInput className="h-4 w-4" />
            Folder Assignment
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Select an account to configure folder assignments
          </p>
        </CardContent>
      </Card>
    );
  }

  const handleAutoDetect = () => {
    autoDetect.mutate(
      { accountId },
      {
        onSuccess: (detected) => {
          setMapping(detected);
          setDirty(true);
        },
      },
    );
  };

  const handleSave = () => {
    updateMapping.mutate(
      { accountId, mapping },
      { onSuccess: () => setDirty(false) },
    );
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2 text-base">
            <FolderInput className="h-4 w-4" />
            Folder Assignment
          </CardTitle>
          <Button
            variant="outline"
            size="sm"
            onClick={handleAutoDetect}
            disabled={autoDetect.isPending}
          >
            {autoDetect.isPending ? (
              <Loader2 className="mr-1 h-3 w-3 animate-spin" />
            ) : (
              <RefreshCw className="mr-1 h-3 w-3" />
            )}
            Auto-detect
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid gap-3">
          {ROLES.map((role) => (
            <div key={role} className="grid grid-cols-[100px_1fr] items-center gap-2">
              <Label className="text-sm">{ROLE_LABELS[role]}</Label>
              <Select
                value={mapping[role] ?? "__none__"}
                onValueChange={(value) => {
                  setMapping((prev) => ({
                    ...prev,
                    [role]: value === "__none__" ? null : value,
                  }));
                  setDirty(true);
                }}
              >
                <SelectTrigger className="h-8">
                  <SelectValue placeholder="Not assigned" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__none__">Not assigned</SelectItem>
                  {folders?.map((f) => (
                    <SelectItem key={f.id} value={f.imap_name}>
                      {f.imap_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          ))}
        </div>

        {dirty && (
          <div className="mt-4 flex justify-end">
            <Button
              onClick={handleSave}
              disabled={updateMapping.isPending}
              size="sm"
            >
              {updateMapping.isPending ? (
                <Loader2 className="mr-1 h-3 w-3 animate-spin" />
              ) : (
                <Save className="mr-1 h-3 w-3" />
              )}
              Save
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
