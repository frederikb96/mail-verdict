"use client";

import { useState } from "react";
import { Archive, ChevronDown, ChevronRight, Package, Send } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  CategoryHeader,
  FieldRow,
  SaveBar,
  SectionCard,
} from "@/components/settings/blocks";
import {
  SettingFields,
  UndeclaredFields,
  presentFields,
  type FieldSpec,
} from "@/components/settings/setting-fields";
import { useSettingsDraft } from "@/hooks/use-settings-draft";

const CATEGORIES = ["mail", "outbox", "orders"];

const FILING: FieldSpec[] = [
  {
    category: "mail",
    key: "mark_read_on_file_to_archive_or_junk",
    label: "Mark as read when filed",
    help: "Archiving a message, or filing it as spam, marks it read as it moves. Mail another client puts in Archive is marked read too.",
    control: "switch",
  },
];

const SENDING: FieldSpec[] = [
  {
    category: "outbox",
    key: "undo_send_seconds",
    label: "Undo send",
    help: "How long a sent message waits before it leaves, so Undo can still take it back.",
    control: "select",
    options: [
      { value: 0, label: "Off" },
      { value: 5, label: "5 seconds" },
      { value: 10, label: "10 seconds" },
      { value: 20, label: "20 seconds" },
      { value: 30, label: "30 seconds" },
    ],
  },
];

const ORDERS: FieldSpec[] = [
  {
    category: "orders",
    key: "language",
    label: "Language of titles and summaries",
    control: "text",
  },
  {
    category: "orders",
    key: "auto_close_days",
    label: "Close an open order after",
    help: "Days after its last mail when no end date was found. 0 never closes one automatically.",
    control: "number",
    min: 0,
    suffix: "days",
  },
  {
    category: "orders",
    key: "auto_close_grace_days",
    label: "Grace after an expected end",
    help: "Days after the later of the expected end and the last mail.",
    control: "number",
    min: 0,
    suffix: "days",
  },
];

const FILTER: FieldSpec = {
  category: "orders",
  key: "filter",
  label: "First filter",
  help: "Regular expressions that decide which mail is even looked at as an order, ticket or booking: include patterns by subject, sender and body, and exclude patterns.",
  control: "json",
};

export function MailSettings() {
  const draft = useSettingsDraft();
  const [showPatterns, setShowPatterns] = useState(false);

  if (!draft.ready) {
    return (
      <>
        <CategoryHeader title="Mail" />
        <Skeleton className="h-32 w-full" />
        <Skeleton className="h-32 w-full" />
      </>
    );
  }

  const hasOrders = presentFields(draft, ORDERS).length > 0;
  const hasFilter = presentFields(draft, [FILTER]).length > 0;

  return (
    <>
      <CategoryHeader title="Mail" />

      <SectionCard icon={Archive} title="Reading and filing">
        {presentFields(draft, FILING).length > 0 ? (
          <SettingFields draft={draft} fields={FILING} />
        ) : (
          <p className="text-sm text-muted-foreground">
            The server didn&apos;t return mail settings -- the interface and the server may be
            running different versions.
          </p>
        )}
        <UndeclaredFields
          draft={draft}
          category="mail"
          area="user"
          declared={["mark_read_on_file_to_archive_or_junk", "bell_badge_counts_new_mail", "notify_wait_seconds"]}
          // Written by the account order list and the unified views list.
          skip={["account_order", "unified_folder_order"]}
        />
      </SectionCard>

      <SectionCard icon={Send} title="Sending">
        <SettingFields draft={draft} fields={SENDING} />
        <UndeclaredFields draft={draft} category="outbox" area="user" declared={["undo_send_seconds"]} />
      </SectionCard>

      {hasOrders && (
        <SectionCard
          icon={Package}
          title="Orders & tickets"
          description="How mail that belongs to an order, ticket or booking is summarised and closed."
        >
          <SettingFields draft={draft} fields={ORDERS} />
          {hasFilter && (
            <div className="flex flex-col gap-2">
              <FieldRow label="First filter" help={FILTER.help}>
                <Button
                  variant="outline"
                  size="sm"
                  aria-expanded={showPatterns}
                  onClick={() => setShowPatterns((v) => !v)}
                >
                  {showPatterns ? (
                    <ChevronDown className="mr-1 h-3.5 w-3.5" />
                  ) : (
                    <ChevronRight className="mr-1 h-3.5 w-3.5" />
                  )}
                  {showPatterns ? "Hide patterns" : "Show patterns"}
                </Button>
              </FieldRow>
              {showPatterns && <SettingFields draft={draft} fields={[{ ...FILTER, label: "Patterns", help: undefined }]} />}
            </div>
          )}
        </SectionCard>
      )}

      {Object.entries(draft.errors).map(
        ([category, message]) =>
          message &&
          CATEGORIES.includes(category) && (
            <p key={category} role="alert" className="text-sm text-destructive">
              Could not save {category} settings: {message}
            </p>
          ),
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
