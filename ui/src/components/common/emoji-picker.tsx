"use client";

import { useState } from "react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

const COMMON_EMOJIS = [
  "\u{1F4E7}", "\u{1F4E8}", "\u{1F4E9}", "\u{1F4EC}", "\u{1F4ED}",
  "\u{1F4EE}", "\u{1F4F0}", "\u{1F3E2}", "\u{1F3E0}", "\u{1F393}",
  "\u{1F4BC}", "\u{1F3AF}", "\u{2B50}", "\u{1F525}", "\u{1F680}",
  "\u{1F308}", "\u{1F30D}", "\u{2764}\u{FE0F}", "\u{1F4A1}", "\u{1F50D}",
  "\u{1F512}", "\u{1F511}", "\u{2699}\u{FE0F}", "\u{1F3F7}\u{FE0F}", "\u{1F4CC}",
  "\u{2705}", "\u{274C}", "\u{26A0}\u{FE0F}", "\u{2603}\u{FE0F}", "\u{1F31F}",
  "\u{1F535}", "\u{1F534}", "\u{1F7E2}", "\u{1F7E1}", "\u{1F7E3}",
];

export function EmojiPicker({
  currentEmoji,
  onSelect,
  label = "Choose emoji",
}: {
  currentEmoji: string | null;
  onSelect: (emoji: string | null) => void;
  label?: string;
}) {
  const [isOpen, setIsOpen] = useState(false);

  return (
    <Popover open={isOpen} onOpenChange={setIsOpen}>
      <PopoverTrigger
        render={
          <button
            type="button"
            className="flex h-9 w-9 items-center justify-center rounded-md border bg-background text-lg hover:bg-accent"
            title={label}
            aria-label={label}
          />
        }
      >
        {currentEmoji || "\u{2795}"}
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="grid w-[16.5rem] max-w-[calc(100vw-2rem)] grid-cols-7 gap-1 p-2"
      >
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
      </PopoverContent>
    </Popover>
  );
}
