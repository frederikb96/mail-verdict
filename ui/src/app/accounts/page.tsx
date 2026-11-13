import { AccountsSettings } from "@/components/settings/categories/accounts";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function AccountsRoute() {
  return (
    <SettingsShell area="settings" category="accounts">
      <AccountsSettings />
    </SettingsShell>
  );
}
