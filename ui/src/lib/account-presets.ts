/** Mail providers the add-account form fills in: server settings, how the
 * sign-in name is derived from the address, and where the provider's own
 * pages explain the app password it requires. */

import type { AccountCreateRequest } from "@/types/api";

export type AccountPresetId = "gmail" | "icloud";

export interface AccountPreset {
  id: AccountPresetId;
  label: string;
  imapHost: string;
  imapPort: number;
  smtpHost: string;
  smtpPort: number;
  /** The name IMAP signs in with, from the email address. */
  imapUser: (email: string) => string;
  /** What has to be switched on before the provider offers app passwords. */
  prerequisite: string;
  appPasswordLink: { label: string; href: string };
  /** One line each, shown under the preset. */
  notes: string[];
}

const fullAddress = (email: string): string => email;
const beforeAt = (email: string): string => email.split("@")[0];

export const ACCOUNT_PRESETS: AccountPreset[] = [
  {
    id: "gmail",
    label: "Gmail",
    imapHost: "imap.gmail.com",
    imapPort: 993,
    smtpHost: "smtp.gmail.com",
    smtpPort: 465,
    imapUser: fullAddress,
    prerequisite: "Turn on 2-Step Verification for the Google Account first.",
    appPasswordLink: {
      label: "How Google app passwords work",
      href: "https://support.google.com/accounts/answer/185833",
    },
    notes: [
      "Sign in with a 16-digit app password, not the account password.",
      "Google Workspace (work or school) accounts cannot be connected: Google allows only OAuth for them, which MailVerdict does not support.",
      "Gmail's label folders hold copies of mail that is also in the inbox, so the AI skips them; All Mail is treated as the archive. Each folder's switch is under Folder Order & Visibility.",
      "Google caps how much IMAP can download per day, so a large mailbox can take a few days to sync completely.",
    ],
  },
  {
    id: "icloud",
    label: "iCloud",
    imapHost: "imap.mail.me.com",
    imapPort: 993,
    smtpHost: "smtp.mail.me.com",
    smtpPort: 587,
    imapUser: beforeAt,
    prerequisite: "Two-factor authentication must be on for the Apple Account.",
    appPasswordLink: {
      label: "How Apple app-specific passwords work",
      href: "https://support.apple.com/en-us/102654",
    },
    notes: [
      "Sign in with an app-specific password, not the Apple Account password.",
      "Signs in to IMAP with the part before the @ and to SMTP with the full address, as Apple documents. If the account stays in error, add it again under Other server with the full address as the IMAP user.",
      "Changing the Apple Account password revokes every app-specific password; the account then needs a new one.",
    ],
  },
];

export function presetById(id: AccountPresetId): AccountPreset {
  const preset = ACCOUNT_PRESETS.find((p) => p.id === id);
  if (!preset) throw new Error(`Unknown account preset: ${id}`);
  return preset;
}

/** The connection part of an add-account request for a preset: the same
 * app password signs in to IMAP and SMTP. */
export function presetConnection(
  preset: AccountPreset,
  email: string,
  appPassword: string,
): Pick<
  AccountCreateRequest,
  | "imap_host"
  | "imap_port"
  | "imap_user"
  | "imap_password"
  | "smtp_host"
  | "smtp_port"
  | "smtp_user"
  | "smtp_password"
> {
  const address = email.trim();
  return {
    imap_host: preset.imapHost,
    imap_port: preset.imapPort,
    imap_user: preset.imapUser(address),
    imap_password: appPassword,
    smtp_host: preset.smtpHost,
    smtp_port: preset.smtpPort,
    smtp_user: address,
    smtp_password: appPassword,
  };
}
