import { SecretsSettings } from "@/components/settings/categories/admin-secrets";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function SecretsRoute() {
  return (
    <SettingsShell area="admin" category="secrets">
      <SecretsSettings />
    </SettingsShell>
  );
}
