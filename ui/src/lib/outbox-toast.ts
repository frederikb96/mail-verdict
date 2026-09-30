import type { OutboxKind, OutboxStatus } from "@/types/api";

export type OutboxToast = { message: string; variant: "success" | "warning" | "error" } | null;

const OUTBOX_TOAST: Record<OutboxStatus, OutboxToast> = {
  pending: null,
  processing: null,
  sent: { message: "Message sent", variant: "success" },
  failed: { message: "Sending failed, retrying", variant: "warning" },
  dead: {
    message: "Could not send message — check SMTP settings on this account",
    variant: "error",
  },
};

/** kind="append" is the glacier's own restore mechanism -- it transmits
 * nothing and opens no SMTP connection, so the ordinary send/failure
 * wording above (which names SMTP outright) is simply wrong for it. */
const GLACIER_RESTORE_TOAST: Record<OutboxStatus, OutboxToast> = {
  pending: null,
  processing: null,
  sent: { message: "Message restored", variant: "success" },
  failed: { message: "Restoring this message failed, retrying", variant: "warning" },
  dead: { message: "Could not restore this message", variant: "error" },
};

/** Which toast (if any) an outbox.updated event's status/kind pair earns
 * -- a restore (kind="append") never says "sent" or "SMTP", since it
 * neither composes nor transmits anything. */
export function outboxToastFor(status: OutboxStatus, kind: OutboxKind | undefined): OutboxToast {
  return kind === "append" ? GLACIER_RESTORE_TOAST[status] : OUTBOX_TOAST[status];
}
