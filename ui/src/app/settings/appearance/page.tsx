import { AppearanceSettings } from "@/components/settings/categories/appearance";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function AppearanceRoute() {
  return (
    <SettingsShell area="settings" category="appearance">
      <AppearanceSettings />
    </SettingsShell>
  );
}
