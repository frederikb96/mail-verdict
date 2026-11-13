import { ModelsSettings } from "@/components/settings/categories/admin-models";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function ModelsRoute() {
  return (
    <SettingsShell area="admin" category="models">
      <ModelsSettings />
    </SettingsShell>
  );
}
