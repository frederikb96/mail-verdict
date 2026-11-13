"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useIsAdmin } from "@/hooks/use-me";
import { useToast } from "@/hooks/use-toast";
import { useSidebar } from "@/components/ui/sidebar";
import { useShortcutBindings } from "@/lib/shortcut-bindings";
import { matchAction, yieldsToField, type ShortcutActionId } from "@/lib/shortcuts";
import { isEditableElement } from "@/lib/utils";

/** Where each go-to shortcut leads -- Ctrl+Alt+<n> by default, numbered in
 * the sidebar's own order the way desktop mail clients number theirs. */
const ROUTES: Partial<Record<ShortcutActionId, string>> = {
  go_mail: "/",
  go_calendar: "/calendar",
  go_contacts: "/contacts",
  go_orders: "/orders",
  go_spam_review: "/spam-review",
  go_search: "/search",
  go_accounts: "/settings/accounts",
  go_pipeline: "/admin/processing",
  go_settings: "/settings",
};

/** Shortcuts whose page is the administrators' alone. */
const ADMIN_ROUTES: ReadonlySet<ShortcutActionId> = new Set(["go_pipeline"]);

/** The go-to and sidebar shortcuts, from any page. Focusing the search
 * field and the cheat sheet are handled by the components they open. */
export function GlobalShortcuts() {
  const router = useRouter();
  const { toggleSidebar } = useSidebar();
  const bindings = useShortcutBindings();
  const isAdmin = useIsAdmin();
  const { push: pushToast } = useToast();

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const action = matchAction(event, bindings, "global");
      if (!action) return;
      if (isEditableElement(event.target) && yieldsToField(event, bindings[action]!)) return;
      if (action === "toggle_sidebar") {
        event.preventDefault();
        toggleSidebar();
        return;
      }
      const target = ROUTES[action];
      if (!target) return;
      event.preventDefault();
      if (ADMIN_ROUTES.has(action) && !isAdmin) {
        pushToast("Administrators only", "info");
        return;
      }
      router.push(target);
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [router, toggleSidebar, bindings, isAdmin, pushToast]);

  return null;
}
