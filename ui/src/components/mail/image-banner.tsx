"use client";

import { ImageOff, Shield } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useCreateImageException } from "@/hooks/use-image-exceptions";

interface ImageBannerProps {
  accountId: string;
  senderEmail: string | null;
  senderDomain: string | null;
  imagesAllowed: boolean;
  hasBlockedImages: boolean;
  onLoadForMessage: () => void;
}

/**
 * Banner shown when remote images are blocked in an email.
 * Offers three options: load once, always for sender, always for domain.
 */
export function ImageBanner({
  accountId,
  senderEmail,
  senderDomain,
  imagesAllowed,
  hasBlockedImages,
  onLoadForMessage,
}: ImageBannerProps) {
  const createException = useCreateImageException();

  if (!hasBlockedImages || imagesAllowed) return null;

  return (
    <div className="flex flex-wrap items-center gap-2 border-b bg-amber-500/10 px-4 py-2 text-sm text-amber-700 dark:text-amber-400">
      <div className="flex items-center gap-1.5">
        <ImageOff className="h-4 w-4" />
        <span>Remote images blocked for privacy</span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        <Button
          variant="outline"
          size="sm"
          className="h-7 text-xs"
          onClick={onLoadForMessage}
        >
          Load for this message
        </Button>
        {senderEmail && (
          <Button
            variant="outline"
            size="sm"
            className="h-7 text-xs"
            disabled={createException.isPending}
            onClick={() =>
              createException.mutate({
                accountId,
                data: { type: "sender", value: senderEmail },
              })
            }
          >
            <Shield className="mr-1 h-3 w-3" />
            Always from {senderEmail}
          </Button>
        )}
        {senderDomain && (
          <Button
            variant="outline"
            size="sm"
            className="h-7 text-xs"
            disabled={createException.isPending}
            onClick={() =>
              createException.mutate({
                accountId,
                data: { type: "domain", value: senderDomain },
              })
            }
          >
            <Shield className="mr-1 h-3 w-3" />
            Always from @{senderDomain}
          </Button>
        )}
      </div>
    </div>
  );
}
