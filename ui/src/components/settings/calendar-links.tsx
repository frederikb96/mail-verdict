"use client";

/**
 * The identity-to-calendar mapping: one row per identity, a column of
 * calendar checkboxes, and a radio for which checked calendar receives
 * invitations addressed to that identity. Dirty-tracked and saved like
 * `CategorySettings` on this same page; a `PUT` names `base_revision` so a
 * conflicting concurrent edit is rejected rather than silently overwritten.
 */

import { useEffect, useState } from "react";
import { Code, Loader2, Save, Table2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useCalendarLinks, useCalendars, useUpdateCalendarLinks } from "@/hooks/use-calendars";
import { useToast } from "@/hooks/use-toast";
import { resolveCalendarColor } from "@/components/calendar/colors";
import type { CalendarLinkRow } from "@/types/api";

export function CalendarLinksCard() {
  const { data: links, isLoading } = useCalendarLinks();
  const { data: calendars } = useCalendars();
  const updateLinks = useUpdateCalendarLinks();
  const { push: pushToast } = useToast();

  const [rows, setRows] = useState<CalendarLinkRow[]>([]);
  const [dirty, setDirty] = useState(false);
  const [rawMode, setRawMode] = useState(false);
  const [rawText, setRawText] = useState("");
  const [rawError, setRawError] = useState<string | null>(null);
  const [invalidIdentityId, setInvalidIdentityId] = useState<string | null>(null);

  useEffect(() => {
    if (!links) return;
    setRows(links.rows);
    setRawText(JSON.stringify(links.rows, null, 2));
    setDirty(false);
  }, [links]);

  if (isLoading || !links) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Calendar invitations</CardTitle>
        </CardHeader>
        <CardContent>
          <Skeleton className="h-32 w-full" />
        </CardContent>
      </Card>
    );
  }

  if (rows.length === 0) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Calendar invitations</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            No sending identities yet -- add one on the Accounts page to link it to a calendar.
          </p>
        </CardContent>
      </Card>
    );
  }

  const toggleCalendar = (identityId: string, calendarId: string, checked: boolean) => {
    setRows((prev) =>
      prev.map((r) => {
        if (r.identity_id !== identityId) return r;
        const calendar_ids = checked
          ? Array.from(new Set([...r.calendar_ids, calendarId]))
          : r.calendar_ids.filter((id) => id !== calendarId);
        const receives_invitations_calendar_id = calendar_ids.includes(
          r.receives_invitations_calendar_id ?? "",
        )
          ? r.receives_invitations_calendar_id
          : (calendar_ids[0] ?? null);
        return { ...r, calendar_ids, receives_invitations_calendar_id };
      }),
    );
    setDirty(true);
  };

  const setReceiving = (identityId: string, calendarId: string) => {
    setRows((prev) =>
      prev.map((r) =>
        r.identity_id === identityId ? { ...r, receives_invitations_calendar_id: calendarId } : r,
      ),
    );
    setDirty(true);
  };

  const handleSave = () => {
    setInvalidIdentityId(null);
    let payloadRows = rows;
    if (rawMode) {
      try {
        payloadRows = JSON.parse(rawText);
        setRawError(null);
      } catch {
        setRawError("Invalid JSON");
        return;
      }
    }
    updateLinks.mutate(
      {
        base_revision: links.base_revision,
        rows: payloadRows.map((r) => ({
          identity_id: r.identity_id,
          calendar_ids: r.calendar_ids,
          receives_invitations_calendar_id: r.receives_invitations_calendar_id,
        })),
      },
      {
        onSuccess: () => setDirty(false),
        onError: (err) => {
          // A 422 names the offending identity so its row can be
          // highlighted -- best-effort parse, since the shape is the
          // backend's error body, not a typed response.
          try {
            const body = JSON.parse(err.message) as { identity_id?: string };
            if (body.identity_id) setInvalidIdentityId(body.identity_id);
          } catch {
            // Not JSON -- nothing to highlight.
          }
          pushToast(`Could not save: ${err.message}`, "error", 0);
        },
      },
    );
  };

  const groupedByAccount = new Map<string, CalendarLinkRow[]>();
  for (const r of rows) {
    if (!groupedByAccount.has(r.account_id)) groupedByAccount.set(r.account_id, []);
    groupedByAccount.get(r.account_id)!.push(r);
  }

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between pb-3">
        <CardTitle className="text-base">Calendar invitations</CardTitle>
        <Button variant="ghost" size="icon-sm" onClick={() => setRawMode(!rawMode)}>
          {rawMode ? <Table2 className="h-4 w-4" /> : <Code className="h-4 w-4" />}
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {!rawMode ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b text-left text-xs text-muted-foreground">
                  <th className="py-1 pr-2">Identity</th>
                  {calendars?.map((c) => (
                    <th key={c.id} className="px-1 py-1 text-center">
                      <span
                        className="mx-auto block h-2.5 w-2.5 rounded-full"
                        style={{ background: resolveCalendarColor(c) }}
                        title={c.display_name}
                      />
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {Array.from(groupedByAccount.entries()).flatMap(([, identityRows]) =>
                  identityRows.map((r) => (
                    <tr
                      key={r.identity_id}
                      className={
                        invalidIdentityId === r.identity_id
                          ? "border-b bg-destructive/10"
                          : "border-b"
                      }
                    >
                      <td className="py-1.5 pr-2">{r.identity_address}</td>
                      {calendars?.map((c) => (
                        <td key={c.id} className="px-1 text-center">
                          <div className="flex flex-col items-center gap-0.5">
                            <Checkbox
                              checked={r.calendar_ids.includes(c.id)}
                              disabled={c.read_only}
                              onCheckedChange={(checked) =>
                                toggleCalendar(r.identity_id, c.id, checked === true)
                              }
                            />
                            {r.calendar_ids.includes(c.id) && (
                              <input
                                type="radio"
                                name={`receiving-${r.identity_id}`}
                                checked={r.receives_invitations_calendar_id === c.id}
                                onChange={() => setReceiving(r.identity_id, c.id)}
                                className="h-3 w-3"
                                title="Receives invitations"
                              />
                            )}
                          </div>
                        </td>
                      ))}
                    </tr>
                  )),
                )}
              </tbody>
            </table>
            <p className="mt-2 text-xs text-muted-foreground">
              Checkbox links a calendar to the identity; the radio button below it picks which one
              new invitations import into.
            </p>
          </div>
        ) : (
          <div className="flex flex-col gap-1.5">
            <Textarea
              value={rawText}
              onChange={(e) => {
                setRawText(e.target.value);
                setDirty(true);
              }}
              rows={12}
              className="font-mono text-xs"
            />
            {rawError && <p className="text-xs text-destructive">{rawError}</p>}
          </div>
        )}

        {dirty && (
          <div className="flex justify-end">
            <Button onClick={handleSave} disabled={updateLinks.isPending}>
              {updateLinks.isPending ? (
                <Loader2 className="mr-1 h-4 w-4 animate-spin" />
              ) : (
                <Save className="mr-1 h-4 w-4" />
              )}
              Save
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
