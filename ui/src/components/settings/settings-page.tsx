"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Save, Loader2, Bot, CalendarDays, Repeat, Sparkles, Sun, Moon, Monitor, Workflow, Undo2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";

import { useAllSettings, useUpdateSettings } from "@/hooks/use-settings";
import { useTheme } from "@/components/theme-provider";
import { UnifiedOrder } from "@/components/settings/unified-order";
import { CalendarLinksCard } from "@/components/settings/calendar-links";
import { DefaultCalendarSetting } from "@/components/settings/default-calendar-setting";

/**
 * Settings groups into three things Freddy actually goes looking for,
 * rather than one long unlabelled scroll of cards: Appearance (how the
 * interface looks), Mail (cross-account behaviour), Calendar (everything
 * about invitations and events in one place, where it used to be split
 * across a raw card above the tabs and a tab below them), and AI &
 * automation for the categories that are closer to server configuration
 * than to a setting most people touch day to day.
 *
 * Every category the server actually has (`SettingCategory` in
 * settings/defaults.py) gets a tab here -- "semantic" and "pipeline" had
 * none before, reachable only by calling the API directly. "spam" is the
 * opposite case: it has a tab-shaped hole in `CATEGORIES` below, on
 * purpose, as the comment there explains.
 */
const CATEGORIES = [
  { key: "ai", label: "AI", icon: Bot },
  { key: "semantic", label: "Semantic search", icon: Sparkles },
  { key: "retry", label: "Retry", icon: Repeat },
  { key: "pipeline", label: "Pipeline", icon: Workflow },
  { key: "outbox", label: "Outbox", icon: Undo2 },
] as const;

// ai's read-only credential status: computed on every GET from whether a
// provider key is stored, never a setting itself -- a PUT strips it before
// merging (settings_api.py's _apply_credential_writes), so editing it here
// would silently do nothing. The key fields themselves (anthropic_api_key,
// openai_api_key) never appear in a GET response at all -- they are
// write-only, so the generic renderer below never draws them either way;
// ProviderKeySettings is their own dedicated form.
const COMPUTED_SETTINGS: Record<string, string[]> = {
  ai: [
    "anthropic_api_key_configured",
    "anthropic_api_key_hint",
    "openai_api_key_configured",
    "openai_api_key_hint",
  ],
  // Rendered by DefaultCalendarSetting instead, below -- a bare calendar
  // id typed into a text box is not a control anyone can use; it needs
  // the same enabled/writable calendar list the event editor itself
  // offers.
  calendar: ["default_calendar_id"],
};

/** A raw settings key read as a sentence rather than the key itself --
 * "default_event_duration_minutes" reads as "Default event duration
 * minutes". Display only, in the generic renderer that draws whatever
 * fields a category's GET happens to return; onChange still keys by the
 * real name. The raw key stays reachable as the label's title attribute,
 * for cross-referencing against the API/config docs, which use it. */
function humanizeSettingKey(key: string): string {
  const [first, ...rest] = key.split("_");
  return [first.charAt(0).toUpperCase() + first.slice(1), ...rest].join(" ");
}

function SettingField({
  name,
  value,
  onChange,
}: {
  name: string;
  value: unknown;
  onChange: (name: string, value: unknown) => void;
}) {
  if (typeof value === "boolean") {
    return (
      <div className="flex items-center justify-between">
        <Label className="text-sm" title={name}>{humanizeSettingKey(name)}</Label>
        <input
          type="checkbox"
          checked={value}
          onChange={(e) => onChange(name, e.target.checked)}
          className="h-4 w-4"
        />
      </div>
    );
  }

  if (typeof value === "number") {
    return (
      <div className="grid gap-1.5">
        <Label className="text-sm" title={name}>{humanizeSettingKey(name)}</Label>
        <Input
          type="number"
          value={value}
          onChange={(e) => onChange(name, Number(e.target.value))}
        />
      </div>
    );
  }

  if (typeof value === "object" && value !== null) {
    return (
      <div className="grid gap-1.5">
        <Label className="text-sm" title={name}>{humanizeSettingKey(name)}</Label>
        <Textarea
          value={JSON.stringify(value, null, 2)}
          rows={4}
          onChange={(e) => {
            try {
              onChange(name, JSON.parse(e.target.value));
            } catch {
              // Allow invalid JSON during editing
            }
          }}
        />
      </div>
    );
  }

  // String or password
  const isPassword =
    name.toLowerCase().includes("key") ||
    name.toLowerCase().includes("password") ||
    name.toLowerCase().includes("secret");

  return (
    <div className="grid gap-1.5">
      <Label className="text-sm">{humanizeSettingKey(name)}</Label>
      <Input
        type={isPassword ? "password" : "text"}
        value={String(value ?? "")}
        onChange={(e) => onChange(name, e.target.value)}
      />
    </div>
  );
}

function CategorySettings({
  category,
  settings,
}: {
  category: string;
  settings: Record<string, unknown>;
}) {
  const [localSettings, setLocalSettings] = useState(settings);
  const [dirty, setDirty] = useState(false);
  const updateSettings = useUpdateSettings();

  useEffect(() => {
    setLocalSettings(settings);
    setDirty(false);
  }, [settings]);

  const handleChange = useCallback((name: string, value: unknown) => {
    setLocalSettings((prev) => ({ ...prev, [name]: value }));
    setDirty(true);
  }, []);

  const handleSave = () => {
    updateSettings.mutate(
      { category, data: localSettings },
      { onSuccess: () => setDirty(false) },
    );
  };

  const computed = COMPUTED_SETTINGS[category] ?? [];
  const entries = Object.entries(localSettings).filter(
    ([key]) => key !== "id" && key !== "category" && !computed.includes(key),
  );

  return (
    <div className="flex flex-col gap-4">
      {entries.map(([key, value]) => (
        <SettingField key={key} name={key} value={value} onChange={handleChange} />
      ))}
      {entries.length === 0 && (
        <div className="py-4 text-sm text-muted-foreground">
          No settings in this category
        </div>
      )}
      {dirty && (
        <div className="flex justify-end">
          <Button onClick={handleSave} disabled={updateSettings.isPending}>
            {updateSettings.isPending ? (
              <Loader2 className="mr-1 h-4 w-4 animate-spin" />
            ) : (
              <Save className="mr-1 h-4 w-4" />
            )}
            Save
          </Button>
        </div>
      )}
    </div>
  );
}

/**
 * One provider's key: masked input, Save, and Clear -- the form
 * `CategorySettings`'s generic renderer cannot offer, since the field it
 * writes (`${provider}_api_key`) never appears in a GET (write-only) and
 * the read-only status fields it renders instead are excluded from it by
 * `COMPUTED_SETTINGS`.
 *
 * The input always starts empty -- there is nothing to prefill it with --
 * so an unmodified, untouched field is never sent on Save; only typing
 * (or pressing Clear) marks it dirty. That is what stops an idle Save
 * from wiping out a key nobody meant to touch.
 */
function ProviderKeyField({
  providerKey,
  label,
  settings,
}: {
  providerKey: string;
  label: string;
  settings: Record<string, unknown>;
}) {
  const [value, setValue] = useState("");
  const [touched, setTouched] = useState(false);
  const updateSettings = useUpdateSettings();

  const configured = settings[`${providerKey}_api_key_configured`] === true;
  const hint = settings[`${providerKey}_api_key_hint`];
  const fieldName = `${providerKey}_api_key`;

  const save = (nextValue: string) => {
    updateSettings.mutate(
      { category: "ai", data: { [fieldName]: nextValue } },
      { onSuccess: () => { setValue(""); setTouched(false); } },
    );
  };

  return (
    <div className="flex flex-col gap-1.5 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex items-baseline gap-2">
        <span className="font-medium">{label}</span>
        <span className="text-xs text-muted-foreground">
          {configured ? `configured (…${hint})` : "not configured"}
        </span>
      </div>
      <div className="flex gap-2">
        <Input
          type="password"
          autoComplete="off"
          aria-label={`${label} API key`}
          placeholder={configured ? "Replace key" : "Paste key"}
          value={value}
          onChange={(e) => { setValue(e.target.value); setTouched(true); }}
          className="w-56"
        />
        <Button
          size="sm"
          variant="outline"
          aria-label={`Store the ${label} key`}
          onClick={() => save(value)}
          disabled={!touched || updateSettings.isPending}
        >
          Save
        </Button>
        {configured && (
          <Button
            size="sm"
            variant="ghost"
            aria-label={`Remove the ${label} key`}
            onClick={() => save("")}
            disabled={updateSettings.isPending}
          >
            Clear
          </Button>
        )}
      </div>
    </div>
  );
}

/** Every provider key, each its own field -- the fields `CategorySettings`
 * deliberately excludes above. */
function ProviderKeySettings({ settings }: { settings: Record<string, unknown> }) {
  const providers: { key: string; label: string }[] = [
    { key: "anthropic", label: "Anthropic" },
    { key: "openai", label: "OpenAI" },
  ];
  return (
    <div className="flex flex-col gap-3 rounded-md border bg-muted/30 p-3 text-sm">
      {providers.map(({ key, label }) => (
        <ProviderKeyField key={key} providerKey={key} label={label} settings={settings} />
      ))}
      <p className="text-xs text-muted-foreground">
        Keys are encrypted at rest and never shown again once saved -- clearing one falls back to
        the matching environment variable, if set.
      </p>
    </div>
  );
}

function ThemeSettings() {
  const { theme, setTheme } = useTheme();

  return (
    <div className="grid gap-3">
      <Label className="text-sm">Theme</Label>
      <div className="flex gap-2">
        {[
          { value: "light" as const, icon: Sun, label: "Light" },
          { value: "dark" as const, icon: Moon, label: "Dark" },
          { value: "system" as const, icon: Monitor, label: "System" },
        ].map(({ value, icon: Icon, label }) => (
          <Button
            key={value}
            variant={theme === value ? "default" : "outline"}
            size="sm"
            onClick={() => setTheme(value)}
          >
            <Icon className="mr-1 h-3 w-3" />
            {label}
          </Button>
        ))}
      </div>
    </div>
  );
}

function SectionHeading({ children }: { children: React.ReactNode }) {
  return <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wide">{children}</h2>;
}

export function SettingsPage() {
  const { data: allSettings, isLoading } = useAllSettings();

  if (isLoading) {
    return (
      <div className="flex flex-col gap-4 p-6">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-8 p-6">
      <h1 className="text-2xl font-semibold">Settings</h1>

      <div className="flex flex-col gap-3">
        <SectionHeading>Appearance</SectionHeading>
        <Card>
          <CardContent className="pt-6">
            <ThemeSettings />
          </CardContent>
        </Card>
      </div>

      <div className="flex flex-col gap-3">
        <SectionHeading>Mail</SectionHeading>
        <UnifiedOrder />
      </div>

      <div className="flex flex-col gap-3">
        <SectionHeading>Calendar</SectionHeading>
        <CalendarLinksCard />
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <CalendarDays className="h-4 w-4" />
              Event defaults
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            {allSettings?.calendar ? (
              <>
                <DefaultCalendarSetting
                  value={
                    typeof allSettings.calendar.default_calendar_id === "string"
                      ? allSettings.calendar.default_calendar_id
                      : ""
                  }
                />
                <CategorySettings category="calendar" settings={allSettings.calendar} />
              </>
            ) : (
              <div className="py-4 text-sm text-muted-foreground">
                The server didn&apos;t return calendar settings -- the interface and the server
                may be running different versions.
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="flex flex-col gap-3">
        <SectionHeading>AI &amp; automation</SectionHeading>
        <div className="flex items-start gap-2 rounded-md border bg-muted/30 p-3 text-sm">
          <Workflow className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
          <div>
            Whether spam detection runs, and what happens to a message it flags, are stages in
            the{" "}
            <Link href="/pipeline" className="underline">
              pipeline
            </Link>{" "}
            now, not a setting here -- a <span className="font-mono">classify</span> stage
            produces the verdict, a <span className="font-mono">match</span> stage decides what
            happens to it. The categories below are the model that stage calls (AI), how it
            finds similar past messages (Semantic search), how a failed step is retried
            (Retry), and the worker mechanics behind the queue (Pipeline).
          </div>
        </div>
        <Tabs defaultValue="ai">
          <TabsList>
            {CATEGORIES.map(({ key, label, icon: Icon }) => (
              <TabsTrigger key={key} value={key} className="gap-1.5">
                <Icon className="h-3.5 w-3.5" />
                {label}
              </TabsTrigger>
            ))}
          </TabsList>
          {CATEGORIES.map(({ key }) => (
            <TabsContent key={key} value={key}>
              <Card>
                <CardContent className="flex flex-col gap-4 pt-6">
                  {key === "ai" && allSettings?.ai && (
                    <ProviderKeySettings settings={allSettings.ai} />
                  )}
                  {allSettings?.[key] ? (
                    <CategorySettings category={key} settings={allSettings[key]} />
                  ) : (
                    <div className="py-4 text-sm text-muted-foreground">
                      The server didn&apos;t return settings for this category -- the interface
                      and the server may be running different versions.
                    </div>
                  )}
                </CardContent>
              </Card>
            </TabsContent>
          ))}
        </Tabs>
      </div>
    </div>
  );
}
