import { NotificationsSettings } from "@/components/settings/categories/notifications";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function NotificationsRoute() {
  return (
    <SettingsShell area="settings" category="notifications">
      <NotificationsSettings />
    </SettingsShell>
  );
}
