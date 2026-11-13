import { CalendarSettings } from "@/components/settings/categories/calendar";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function CalendarRoute() {
  return (
    <SettingsShell area="settings" category="calendar">
      <CalendarSettings />
    </SettingsShell>
  );
}
