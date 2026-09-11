"use client";

/** The initials-in-a-circle avatar rendered by every list row that shows a
 * person or account -- mail rows, contact rows, thread messages, search
 * results. One place to add a real image or a colour derived from the
 * sender, instead of the same markup duplicated at each call site.
 *
 * A photo pulled from the address book takes precedence here once one
 * exists to read. The `photoUrl` prop is resolved by the caller from
 * wherever that lookup lives -- a contact row already showing its own
 * contact passes its photo directly, a thread message resolves the
 * sender's address against the contact API. This component never
 * fetches anything itself, and a caller must never pass a photo whose
 * `kind` is `"url"` without first running it through the same
 * remote-content allowlist any other remote image does -- unlike an
 * embedded `data:` photo, it is a request to whatever the URL names.
 * Deliberately never a lookup keyed by address against an unrelated
 * third party, e.g. Gravatar -- that would tell that party a message
 * from the address was opened, for every sender, which is not what the
 * allowlist governs. */

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { avatarColorFor } from "@/lib/avatar-color";
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
   * contact's photo -- already allowlist-checked by the caller if it came
   * from a `kind: "url"` source. */
  photoUrl?: string | null;
  /** What the fallback's colour is derived from -- an email address where
   * the caller has one, since a display name alone can collide across
   * senders. Omitted keeps the plain neutral fallback, for a caller that
   * has no stable identity to hash (a generic placeholder, say). */
  colorSeed?: string;
}

export function InitialsAvatar({
  name,
  size = "default",
  className,
  badge,
  photoUrl,
  colorSeed,
}: InitialsAvatarProps) {
  const color = colorSeed ? avatarColorFor(colorSeed) : null;
  return (
    <div className={cn("relative shrink-0", className)}>
      <Avatar size={size}>
        {photoUrl && <AvatarImage src={photoUrl} />}
        <AvatarFallback
          className={color ? "text-[color:var(--avatar-color)]" : undefined}
          style={
            color
              ? ({
                  "--avatar-color": color,
                  backgroundColor: "color-mix(in oklab, var(--avatar-color) 20%, transparent)",
                } as React.CSSProperties)
              : undefined
          }
        >
          {getInitials(name)}
        </AvatarFallback>
      </Avatar>
      {badge && (
        <span className="absolute -bottom-1 -right-1 text-xs leading-none">{badge}</span>
      )}
    </div>
  );
}
