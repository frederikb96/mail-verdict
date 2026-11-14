import { AdminAccessKeys } from "@/components/settings/categories/admin-access-keys";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function AccessKeysRoute() {
  return (
    <SettingsShell area="admin" category="access-keys">
      <AdminAccessKeys />
    </SettingsShell>
  );
}
