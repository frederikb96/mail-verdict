"use client";

/** The initials-in-a-circle avatar rendered by every list row that shows a
 * person or account -- mail rows, contact rows, thread messages, search
 * results. One place to add a real image or a colour derived from the
 * sender, instead of the same markup duplicated at each call site.
 *
 * A photo pulled from the address book takes precedence here once one
 * exists to read. The address book itself carries photos and passes them
 * for its own rows (contact-list.tsx, contact-detail.tsx) -- what's still
 * missing is a mail sender's address resolved to a contact's photo, so a
 * mail row's `photoUrl` stays unset and renders initials regardless. This
 * component never fetches anything itself, resolved or not. Deliberately
 * not a remote avatar keyed by address, e.g. Gravatar -- that would tell
 * a third party a message from that address was opened, for every sender
 * on the remote-image allowlist, which is not what that allowlist is
 * for. */

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { getInitials } from "@/lib/format";
import { cn } from "@/lib/utils";

interface InitialsAvatarProps {
  /** The display name initials are derived from -- sender, contact, whatever the row represents. */
  name: string;
  size?: "default" | "sm" | "lg";
  className?: string;
  /** Rendered over the bottom-right corner, e.g. an emoji identifying the source account. */
  badge?: React.ReactNode;
  /** A resolved photo to show instead of initials, e.g. an address-book
   * contact's photo. Nothing in this app supplies one yet. */
  photoUrl?: string | null;
}

export function InitialsAvatar({
  name,
  size = "default",
  className,
  badge,
  photoUrl,
}: InitialsAvatarProps) {
  return (
    <div className={cn("relative shrink-0", className)}>
      <Avatar size={size}>
        {photoUrl && <AvatarImage src={photoUrl} />}
        <AvatarFallback>{getInitials(name)}</AvatarFallback>
      </Avatar>
      {badge && (
        <span className="absolute -bottom-1 -right-1 text-xs leading-none">{badge}</span>
      )}
    </div>
  );
}
