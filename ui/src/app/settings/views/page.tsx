import { ViewsSettings } from "@/components/settings/categories/views";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function ViewsRoute() {
  return (
    <SettingsShell area="settings" category="views">
      <ViewsSettings />
    </SettingsShell>
  );
}
