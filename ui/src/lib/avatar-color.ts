/** A stable colour for an initials avatar, derived from the identity it
 * represents (an email address where one is available, a display name
 * otherwise) -- the same sender always lands on the same colour, which
 * is what makes scanning a list of grey circles faster. Same djb2-hash-
 * into-a-fixed-palette approach as the calendar's own per-id colour
 * (components/calendar/colors.ts), kept separate rather than shared
 * since the two have no reason to ever pick the same hue for the same
 * id -- a sender and a calendar are unrelated identity spaces. */

const AVATAR_PALETTE = [
  "#3b82f6", // blue
  "#22c55e", // green
  "#f97316", // orange
  "#a855f7", // purple
  "#ec4899", // pink
  "#14b8a6", // teal
  "#eab308", // yellow
  "#ef4444", // red
  "#6366f1", // indigo
  "#84cc16", // lime
  "#06b6d4", // cyan
  "#f43f5e", // rose
] as const;

function djb2(id: string): number {
  let hash = 5381;
  for (let i = 0; i < id.length; i++) {
    hash = (hash * 33 + id.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

/** The colour an initials avatar renders with for a given identity. */
export function avatarColorFor(identity: string): string {
  return AVATAR_PALETTE[djb2(identity) % AVATAR_PALETTE.length];
}
