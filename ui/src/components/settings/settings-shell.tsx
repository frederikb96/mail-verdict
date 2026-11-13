"use client";

/**
 * The frame every settings category lives in: a category list on the left
 * and one category on the right. On a phone the list is a screen of its own
 * and a category opens full-width beneath a back link. `area` picks the
 * personal categories or the administrator's; the administrator's area
 * explains itself to anyone else instead of showing nothing.
 */

import { useEffect, type ReactNode } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ChevronLeft, ChevronRight } from "lucide-react";

import { AdminOnly } from "@/components/common/admin-only";
import { useTheme } from "@/components/theme-provider";
import { useAdminUsers } from "@/hooks/use-admin";
import { useAccounts } from "@/hooks/use-accounts";
import { useIsAdmin, useMe } from "@/hooks/use-me";
import { useProvidersStatus } from "@/hooks/use-settings";
import { useUnifiedFolders } from "@/hooks/use-unified-view";
import { cn } from "@/lib/utils";
import {
  ADMIN_CATEGORIES,
  AREA_TITLE,
  categoriesOf,
  categoryHref,
  type CategoryDef,
  type SettingsArea,
} from "@/components/settings/nav";

const DESKTOP_QUERY = "(min-width: 768px)";

/** What a category row says about its current state, where that is short. */
function CategoryHint({ area, id }: { area: SettingsArea; id: string }) {
  const { theme } = useTheme();
  const { data: views } = useUnifiedFolders();
  const { data: accounts } = useAccounts();
  const { data: providers } = useProvidersStatus();
  const isAdmin = useIsAdmin();
  // Admin-only data is asked for only when an administrator is looking.
  const { data: adminUsers } = useAdminUsers({ enabled: isAdmin && area === "admin" && id === "users" });

  if (area === "settings" && id === "appearance") {
    return <span>{theme.charAt(0).toUpperCase() + theme.slice(1)}</span>;
  }
  if (area === "settings" && id === "views" && views) return <span>{views.length}</span>;
  if (area === "settings" && id === "accounts" && accounts) return <span>{accounts.length}</span>;
  if (area === "admin" && id === "users" && adminUsers) return <span>{adminUsers.length}</span>;
  if (area === "admin" && id === "providers" && isAdmin) {
    const broken = (providers?.breakers ?? []).some((b) => b.state !== "closed");
    if (broken) {
      return (
        <span
          role="img"
          aria-label="A provider is failing"
          className="h-2 w-2 rounded-full bg-destructive"
        />
      );
    }
  }
  return null;
}

function NavItem({
  area,
  def,
  active,
}: {
  area: SettingsArea;
  def: CategoryDef;
  active: boolean;
}) {
  const Icon = def.icon;
  return (
    <Link
      href={categoryHref(area, def.id)}
      prefetch={false}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex items-center gap-3 rounded-lg border px-3 py-3 text-sm md:gap-2 md:border-transparent md:py-1.5",
        active
          ? "md:bg-accent md:font-medium md:text-accent-foreground"
          : "text-foreground md:text-muted-foreground md:hover:bg-accent/60 md:hover:text-accent-foreground",
        def.hideOnTouch && "pointer-coarse:hidden",
      )}
    >
      <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1 truncate">{def.label}</span>
      <span className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground">
        <CategoryHint area={area} id={def.id} />
      </span>
      <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground md:hidden" />
    </Link>
  );
}

function CategoryNav({
  area,
  category,
}: {
  area: SettingsArea;
  category: string | null;
}) {
  const me = useMe();
  const isAdmin = me.role === "admin";
  return (
    <nav aria-label={`${AREA_TITLE[area]} categories`} className="flex flex-col gap-2 md:gap-0.5 md:sticky md:top-4 md:self-start">
      {categoriesOf(area).map((def) => (
        <NavItem key={def.id} area={area} def={def} active={def.id === category} />
      ))}
      {/* On a phone the personal list is the only entry to the rest. */}
      {area === "settings" && isAdmin && (
        <div className="mt-4 flex flex-col gap-2 md:hidden">
          <div className="px-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Administration
          </div>
          {ADMIN_CATEGORIES.map((def) => (
            <NavItem key={def.id} area="admin" def={def} active={false} />
          ))}
        </div>
      )}
      <div className="mt-4 border-t px-1 pt-3 text-xs text-muted-foreground md:mt-6">
        Signed in as <span className="font-medium text-foreground">{me.display_name}</span>
      </div>
    </nav>
  );
}

/** Desktop has no use for an empty pane: the bare area route sends it to
 * the first category. A phone keeps the list. */
function useDesktopRedirect(area: SettingsArea, category: string | null) {
  const router = useRouter();
  useEffect(() => {
    if (category !== null) return;
    if (window.matchMedia(DESKTOP_QUERY).matches) {
      router.replace(categoryHref(area, categoriesOf(area)[0].id));
    }
  }, [area, category, router]);
}

function ShellFrame({
  area,
  category,
  children,
}: {
  area: SettingsArea;
  category: string | null;
  children?: ReactNode;
}) {
  useDesktopRedirect(area, category);
  const def = categoriesOf(area).find((c) => c.id === category);
  const title = AREA_TITLE[area];

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 md:gap-6 md:p-6">
      {def ? (
        <>
          <Link
            href={`/${area}`}
            prefetch={false}
            className="flex items-center gap-1 text-sm text-muted-foreground md:hidden"
          >
            <ChevronLeft className="h-4 w-4" />
            {title}
          </Link>
          <h1 className="hidden text-2xl font-semibold md:block">{title}</h1>
        </>
      ) : (
        <h1 className="text-2xl font-semibold">{title}</h1>
      )}
      <div className="grid gap-6 md:grid-cols-[13rem_minmax(0,1fr)]">
        <div className={cn(def && "hidden md:block")}>
          <CategoryNav area={area} category={category} />
        </div>
        {def && (
          <div
            data-testid="settings-pane"
            data-category={def.id}
            className={cn("flex min-w-0 flex-col gap-4", !def.wide && "max-w-3xl")}
          >
            {children}
          </div>
        )}
      </div>
    </div>
  );
}

export function SettingsShell({
  area,
  category,
  children,
}: {
  area: SettingsArea;
  /** Null for the bare area route, which shows the list alone. */
  category: string | null;
  children?: ReactNode;
}) {
  if (area === "admin") {
    return (
      <AdminOnly>
        <ShellFrame area={area} category={category}>
          {children}
        </ShellFrame>
      </AdminOnly>
    );
  }
  return (
    <ShellFrame area={area} category={category}>
      {children}
    </ShellFrame>
  );
}
