import { SystemSettings } from "@/components/settings/categories/admin-system";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function SystemRoute() {
  return (
    <SettingsShell area="admin" category="system">
      <SystemSettings />
    </SettingsShell>
  );
}
