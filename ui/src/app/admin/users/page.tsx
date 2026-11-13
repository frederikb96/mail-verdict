import { AdminUsers } from "@/components/settings/categories/admin-users";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function UsersRoute() {
  return (
    <SettingsShell area="admin" category="users">
      <AdminUsers />
    </SettingsShell>
  );
}
