"use client";

import { CalendarDays } from "lucide-react";

import { Skeleton } from "@/components/ui/skeleton";
import {
  CategoryHeader,
  FieldRow,
  SaveBar,
  SectionCard,
} from "@/components/settings/blocks";
import { CalendarLinksCard } from "@/components/settings/calendar-links";
import { DefaultCalendarSetting } from "@/components/settings/default-calendar-setting";
import {
  SettingFields,
  UndeclaredFields,
  presentFields,
  type FieldSpec,
} from "@/components/settings/setting-fields";
import { useSettingsDraft } from "@/hooks/use-settings-draft";

const CATEGORIES = ["calendar"];

const DEFAULTS: FieldSpec[] = [
  {
    category: "calendar",
    key: "default_event_duration_minutes",
    label: "Length of a new event",
    help: "Also the step a drag across the grid snaps to.",
    control: "select",
    options: [
      { value: 15, label: "15 minutes" },
      { value: 30, label: "30 minutes" },
      { value: 45, label: "45 minutes" },
      { value: 60, label: "1 hour" },
    ],
  },
  {
    category: "calendar",
    key: "default_reminder_minutes",
    label: "Reminder on a new event",
    help: "Unless the calendar sets its own.",
    control: "select",
    options: [
      { value: 0, label: "At the start" },
      { value: 5, label: "5 minutes before" },
      { value: 10, label: "10 minutes before" },
      { value: 15, label: "15 minutes before" },
      { value: 30, label: "30 minutes before" },
      { value: 60, label: "1 hour before" },
    ],
  },
];

export function CalendarSettings() {
  const draft = useSettingsDraft();
  const calendarId = draft.value("calendar", "default_calendar_id");

  return (
    <>
      <CategoryHeader title="Calendar" />
      <CalendarLinksCard />
      <SectionCard icon={CalendarDays} title="Event defaults">
        {!draft.ready ? (
          <Skeleton className="h-24 w-full" />
        ) : presentFields(draft, DEFAULTS).length > 0 ? (
          <>
            <FieldRow label="Default calendar" htmlFor="default-calendar-select" help="Where a new event is created unless you pick another.">
              <DefaultCalendarSetting
                value={typeof calendarId === "string" ? calendarId : ""}
                onChange={(id) => draft.set("calendar", "default_calendar_id", id)}
              />
            </FieldRow>
            <SettingFields draft={draft} fields={DEFAULTS} />
            <UndeclaredFields
              draft={draft}
              category="calendar"
              area="user"
              declared={DEFAULTS.map((f) => f.key)}
              skip={["default_calendar_id"]}
            />
          </>
        ) : (
          <p className="text-sm text-muted-foreground">
            The server didn&apos;t return calendar settings -- the interface and the server may be
            running different versions.
          </p>
        )}
      </SectionCard>
      {draft.errors.calendar && (
        <p role="alert" className="text-sm text-destructive">
          Could not save calendar settings: {draft.errors.calendar}
        </p>
      )}
      <SaveBar
        dirtyCount={draft.count(CATEGORIES)}
        saving={draft.saving}
        onSave={() => draft.save(CATEGORIES)}
        onDiscard={() => draft.discard(CATEGORIES)}
      />
    </>
  );
}
