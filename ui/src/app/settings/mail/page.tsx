import { MailSettings } from "@/components/settings/categories/mail";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function MailRoute() {
  return (
    <SettingsShell area="settings" category="mail">
      <MailSettings />
    </SettingsShell>
  );
}
