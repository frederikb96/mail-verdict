"use client";

/**
 * Unified view setup: per-account folder unified names and emoji picker.
 *
 * Displayed in the accounts/settings area. Auto-saves with debounce.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

import { useAccounts } from "@/hooks/use-accounts";
import { useFolders } from "@/hooks/use-folders";
import { useUpdateAccountEmoji } from "@/hooks/use-account-emoji";
import { useUpdateUnifiedName } from "@/hooks/use-unified-name";
import type { AccountResponse, FolderResponse } from "@/types/api";

const COMMON_EMOJIS = [
  "\u{1F4E7}", "\u{1F4E8}", "\u{1F4E9}", "\u{1F4EC}", "\u{1F4ED}",
  "\u{1F4EE}", "\u{1F4F0}", "\u{1F3E2}", "\u{1F3E0}", "\u{1F393}",
  "\u{1F4BC}", "\u{1F3AF}", "\u{2B50}", "\u{1F525}", "\u{1F680}",
  "\u{1F308}", "\u{1F30D}", "\u{2764}\u{FE0F}", "\u{1F4A1}", "\u{1F50D}",
  "\u{1F512}", "\u{1F511}", "\u{2699}\u{FE0F}", "\u{1F3F7}\u{FE0F}", "\u{1F4CC}",
  "\u{2705}", "\u{274C}", "\u{26A0}\u{FE0F}", "\u{2603}\u{FE0F}", "\u{1F31F}",
  "\u{1F535}", "\u{1F534}", "\u{1F7E2}", "\u{1F7E1}", "\u{1F7E3}",
];

function EmojiPicker({
  currentEmoji,
  onSelect,
}: {
  currentEmoji: string | null;
  onSelect: (emoji: string | null) => void;
}) {
  const [isOpen, setIsOpen] = useState(false);

  return (
    <div className="relative">
      <button
        type="button"
        className="flex h-9 w-9 items-center justify-center rounded-md border bg-background text-lg hover:bg-accent"
        onClick={() => setIsOpen(!isOpen)}
        title="Choose emoji"
      >
        {currentEmoji || "\u{2795}"}
      </button>
      {isOpen && (
        <div className="absolute left-0 top-10 z-50 grid grid-cols-7 gap-1 rounded-md border bg-popover p-2 shadow-md">
          {currentEmoji && (
            <button
              type="button"
              className="flex h-8 w-8 items-center justify-center rounded text-sm hover:bg-accent"
              onClick={() => {
                onSelect(null);
                setIsOpen(false);
              }}
              title="Clear emoji"
            >
              {"\u{274C}"}
            </button>
          )}
          {COMMON_EMOJIS.map((emoji) => (
            <button
              key={emoji}
              type="button"
              className="flex h-8 w-8 items-center justify-center rounded text-lg hover:bg-accent"
              onClick={() => {
                onSelect(emoji);
                setIsOpen(false);
              }}
            >
              {emoji}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function UnifiedNameField({
  folder,
  accountId,
}: {
  folder: FolderResponse;
  accountId: string;
}) {
  const updateUnifiedName = useUpdateUnifiedName();
  const [value, setValue] = useState(folder.unified_name ?? "");
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Sync external changes
  useEffect(() => {
    setValue(folder.unified_name ?? "");
  }, [folder.unified_name]);

  const handleChange = useCallback(
    (newValue: string) => {
      setValue(newValue);
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => {
        updateUnifiedName.mutate({
          accountId,
          folderId: folder.id,
          unifiedName: newValue || null,
        });
      }, 500);
    },
    [accountId, folder.id, updateUnifiedName],
  );

  return (
    <div className="flex items-center gap-3">
      <span className="min-w-[120px] truncate text-sm text-muted-foreground">
        {folder.imap_name}
      </span>
      <Input
        value={value}
        onChange={(e) => handleChange(e.target.value)}
        placeholder="Unified name..."
        className="h-8 max-w-[200px]"
      />
    </div>
  );
}

function AccountUnifiedSetup({ account }: { account: AccountResponse }) {
  const { data: folders } = useFolders(account.id);
  const updateEmoji = useUpdateAccountEmoji();

  return (
    <div className="flex flex-col gap-3 rounded-md border p-4">
      <div className="flex items-center gap-3">
        <EmojiPicker
          currentEmoji={account.emoji}
          onSelect={(emoji) =>
            updateEmoji.mutate({ accountId: account.id, emoji })
          }
        />
        <span className="font-medium">{account.name}</span>
      </div>
      <div className="flex flex-col gap-2 pl-1">
        <Label className="text-xs text-muted-foreground">
          Folder unified names
        </Label>
        {folders?.map((folder) => (
          <UnifiedNameField
            key={folder.id}
            folder={folder}
            accountId={account.id}
          />
        ))}
        {!folders?.length && (
          <span className="text-sm text-muted-foreground">
            No folders synced yet
          </span>
        )}
      </div>
    </div>
  );
}

export function UnifiedSetup() {
  const { data: accounts } = useAccounts();

  if (!accounts?.length) {
    return (
      <div className="text-sm text-muted-foreground">
        No accounts configured
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <h3 className="text-lg font-medium">Unified View Setup</h3>
        <p className="text-sm text-muted-foreground">
          Assign unified names to folders across accounts. Folders with the same
          unified name merge into one in the Unified View.
        </p>
      </div>
      {accounts.map((account) => (
        <AccountUnifiedSetup key={account.id} account={account} />
      ))}
    </div>
  );
}
