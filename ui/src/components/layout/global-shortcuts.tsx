"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
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
  go_accounts: "/accounts",
  go_pipeline: "/pipeline",
  go_settings: "/settings",
};

/** The go-to and sidebar shortcuts, from any page. Focusing the search
 * field and the cheat sheet are handled by the components they open. */
export function GlobalShortcuts() {
  const router = useRouter();
  const { toggleSidebar } = useSidebar();
  const bindings = useShortcutBindings();

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
      router.push(target);
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [router, toggleSidebar, bindings]);

  return null;
}
