import { cn } from "@/lib/utils"

/** Shortens long text with an end ellipsis by default, or a middle one when
 * `tail` keeps a fixed number of characters visible at the end -- for a
 * filename's extension or an address's domain, where the tail carries
 * information the head does not. CSS can't middle-truncate on its own, and
 * this needs no measurement: it is correct at every width, and degrades to
 * a plain end ellipsis when the string is shorter than `tail`.
 *
 * The hover reveal lives here too, via the native `title` attribute, so it
 * can't be forgotten at a call site -- not the styled `Tooltip`, which is
 * for icon-only controls and carries a focus-manager cost not worth paying
 * on every truncated label in a list. */
export function Truncate({
  text,
  tail = 0,
  className,
}: {
  text: string
  tail?: number
  className?: string
}) {
  if (tail <= 0 || text.length <= tail) {
    return (
      <span className={cn("truncate", className)} title={text}>
        {text}
      </span>
    )
  }
  return (
    <span className={cn("flex min-w-0 items-center", className)} title={text} aria-label={text}>
      <span className="truncate" aria-hidden="true">{text.slice(0, -tail)}</span>
      <span className="shrink-0" aria-hidden="true">{text.slice(-tail)}</span>
    </span>
  )
}
