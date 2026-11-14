import {
  Bell,
  Bot,
  CalendarDays,
  Cpu,
  Keyboard,
  KeyRound,
  KeySquare,
  Layers,
  Mail,
  MonitorSmartphone,
  Palette,
  ShieldCheck,
  UserCircle,
  Users,
  Workflow,
  type LucideIcon,
} from "lucide-react";

export type SettingsArea = "settings" | "admin";

export interface CategoryDef {
  id: string;
  label: string;
  icon: LucideIcon;
  /** Left out of the list on a touch screen, where there is no keyboard. */
  hideOnTouch?: boolean;
  /** The pane uses the full width instead of the reading column. */
  wide?: boolean;
}

/** Personal categories: what only the signed-in person's own mail, look and
 * devices are affected by. */
export const SETTINGS_CATEGORIES: CategoryDef[] = [
  { id: "appearance", label: "Appearance", icon: Palette },
  { id: "mail", label: "Mail", icon: Mail },
  { id: "views", label: "Unified views", icon: Layers },
  { id: "notifications", label: "Notifications", icon: Bell },
  { id: "calendar", label: "Calendar", icon: CalendarDays },
  { id: "accounts", label: "Accounts", icon: UserCircle },
  { id: "sign-ins", label: "Signed-in devices", icon: MonitorSmartphone },
  { id: "shortcuts", label: "Keyboard shortcuts", icon: Keyboard, hideOnTouch: true },
];

/** Instance-wide categories, for administrators: what changes every user's
 * mail, spend or models. */
export const ADMIN_CATEGORIES: CategoryDef[] = [
  { id: "users", label: "Users", icon: Users, wide: true },
  { id: "access-keys", label: "Access keys", icon: KeySquare },
  { id: "models", label: "Models", icon: Bot, wide: true },
  { id: "providers", label: "Providers & keys", icon: ShieldCheck },
  { id: "processing", label: "Processing", icon: Workflow, wide: true },
  { id: "secrets", label: "Secrets", icon: KeyRound },
  { id: "system", label: "System", icon: Cpu },
];

export const AREA_TITLE: Record<SettingsArea, string> = {
  settings: "Settings",
  admin: "Administration",
};

export function categoriesOf(area: SettingsArea): CategoryDef[] {
  return area === "admin" ? ADMIN_CATEGORIES : SETTINGS_CATEGORIES;
}

export function categoryHref(area: SettingsArea, id: string): string {
  return `/${area}/${id}`;
}
