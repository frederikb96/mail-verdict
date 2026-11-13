import { ShortcutsPage } from "@/components/settings/shortcuts-page";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function ShortcutsRoute() {
  return (
    <SettingsShell area="settings" category="shortcuts">
      <ShortcutsPage />
    </SettingsShell>
  );
}
