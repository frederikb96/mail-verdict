import { ProvidersSettings } from "@/components/settings/categories/admin-providers";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function ProvidersRoute() {
  return (
    <SettingsShell area="admin" category="providers">
      <ProvidersSettings />
    </SettingsShell>
  );
}
