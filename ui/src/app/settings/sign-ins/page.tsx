import { SignInsSettings } from "@/components/settings/categories/sign-ins";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function SignInsRoute() {
  return (
    <SettingsShell area="settings" category="sign-ins">
      <SignInsSettings />
    </SettingsShell>
  );
}
