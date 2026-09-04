"use client";

/** The initials-in-a-circle avatar rendered by every list row that shows a
 * person or account -- mail rows, contact rows, thread messages, search
 * results. One place to add a real image or a colour derived from the
 * sender, instead of the same markup duplicated at each call site.
 *
 * A photo pulled from the address book would take precedence here once
 * one exists to read -- nothing in the data model carries a contact photo
 * yet, so callers cannot pass one. */

import { useEffect, useState } from "react";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { getInitials } from "@/lib/format";
import { cn } from "@/lib/utils";
import { gravatarUrl } from "@/lib/gravatar";

const GRAVATAR_PX: Record<"default" | "sm" | "lg", number> = {
  sm: 48,
  default: 64,
  lg: 80,
};

interface InitialsAvatarProps {
  /** The display name initials are derived from -- sender, contact, whatever the row represents. */
  name: string;
  size?: "default" | "sm" | "lg";
  className?: string;
  /** Rendered over the bottom-right corner, e.g. an emoji identifying the source account. */
  badge?: React.ReactNode;
  /** The address a remote avatar would be fetched for. Omitted (the
   * default) renders initials only, with no network involved at all. */
  email?: string | null;
  /** Gates the remote fetch on the same per-sender/domain allowlist that
   * already governs remote images in the message body -- a sender not
   * allowed images from gets no avatar lookup either. Ignored when
   * `email` is not given. */
  imagesAllowed?: boolean;
}

export function InitialsAvatar({
  name,
  size = "default",
  className,
  badge,
  email,
  imagesAllowed = false,
}: InitialsAvatarProps) {
  const [remoteSrc, setRemoteSrc] = useState<string | null>(null);

  useEffect(() => {
    if (!email || !imagesAllowed) {
      setRemoteSrc(null);
      return;
    }
    let cancelled = false;
    void gravatarUrl(email, GRAVATAR_PX[size]).then((url) => {
      if (!cancelled) setRemoteSrc(url);
    });
    return () => {
      cancelled = true;
    };
  }, [email, imagesAllowed, size]);

  return (
    <div className={cn("relative shrink-0", className)}>
      <Avatar size={size}>
        {remoteSrc && <AvatarImage src={remoteSrc} referrerPolicy="no-referrer" />}
        <AvatarFallback>{getInitials(name)}</AvatarFallback>
      </Avatar>
      {badge && (
        <span className="absolute -bottom-1 -right-1 text-xs leading-none">{badge}</span>
      )}
    </div>
  );
}
