"use client";

/** The sidebar's middle group when a calendar route is active -- the
 * mini-month, the calendar list grouped by DAV account with visibility
 * checkboxes, and the manage-calendars trigger. */

import { useAtomValue } from "jotai";
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
} from "@/components/ui/sidebar";
import { Checkbox } from "@/components/ui/checkbox";
import { Badge } from "@/components/ui/badge";
import { MiniMonth } from "@/components/calendar/mini-month";
import { CalendarManageDialog } from "@/components/calendar/calendar-manage-dialog";
import { useCalendars, useUpdateCalendar } from "@/hooks/use-calendars";
import { resolveCalendarColor } from "@/components/calendar/colors";
import { calendarDateAtom } from "@/lib/atoms";

export function CalendarSidebar() {
  const anchor = useAtomValue(calendarDateAtom);
  const { data: calendars } = useCalendars();
  const updateCalendar = useUpdateCalendar();

  const groups = new Map<string, { name: string; calendars: typeof calendars }>();
  for (const c of calendars ?? []) {
    if (!groups.has(c.dav_account_id)) {
      groups.set(c.dav_account_id, { name: c.dav_account_name, calendars: [] });
    }
    groups.get(c.dav_account_id)!.calendars!.push(c);
  }

  return (
    <>
      <SidebarGroup>
        <SidebarGroupContent>
          <MiniMonth anchor={anchor} />
        </SidebarGroupContent>
      </SidebarGroup>

      <SidebarGroup>
        <SidebarGroupLabel className="flex items-center justify-between gap-2 pr-1">
          <span>Calendars</span>
          <CalendarManageDialog />
        </SidebarGroupLabel>
        <SidebarGroupContent>
          <div className="flex flex-col gap-2 px-2">
            {Array.from(groups.entries()).map(([davAccountId, group]) => (
              <div key={davAccountId} className="flex flex-col gap-1">
                <span className="px-1 text-xs text-muted-foreground">{group.name}</span>
                {group.calendars!.map((c) => (
                  <label key={c.id} className="flex items-center gap-2 px-1 py-0.5 text-sm">
                    <Checkbox
                      checked={c.is_visible}
                      onCheckedChange={(checked) =>
                        updateCalendar.mutate({ id: c.id, data: { is_visible: checked === true } })
                      }
                      style={{ accentColor: resolveCalendarColor(c) }}
                    />
                    <span
                      className="h-2.5 w-2.5 shrink-0 rounded-full"
                      style={{ background: resolveCalendarColor(c) }}
                    />
                    <span className="flex-1 truncate">{c.display_name}</span>
                    {c.read_only && (
                      <Badge variant="outline" className="text-[10px]">
                        Read-only
                      </Badge>
                    )}
                  </label>
                ))}
              </div>
            ))}
            {(calendars ?? []).length === 0 && (
              <p className="px-1 py-2 text-sm text-muted-foreground">No calendars yet</p>
            )}
          </div>
        </SidebarGroupContent>
      </SidebarGroup>
    </>
  );
}
