"use client";

import { useCallback, useEffect, useState } from "react";
import { Save, Loader2, Bot, ShieldAlert, Repeat, Sun, Moon, Monitor, Workflow } from "lucide-react";
import Link from "next/link";

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

// "rules" is not a settings category any more -- a rule is a `match` stage
// in the pipeline now, edited through the pipeline definition rather than
// a raw settings JSON blob.
const CATEGORIES = [
  { key: "ai", label: "AI", icon: Bot },
  { key: "spam", label: "Spam", icon: ShieldAlert },
  { key: "retry", label: "Retry", icon: Repeat },
] as const;

// spam.enabled, spam.auto_move_to_junk and spam.auto_mark_read only ever
// fed the one-time migration that built the pipeline's first revision --
// classification is now a `classify` stage, and moving/marking spam is a
// `match` stage the pipeline page edits directly. spam.excerpt_length has
// no reader left either. Editing any of them here would silently do
// nothing, which is worse than not offering the control.
const DEAD_SETTINGS: Record<string, string[]> = {
  spam: ["enabled", "auto_move_to_junk", "auto_mark_read", "excerpt_length"],
};

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
        <Label className="text-sm">{name}</Label>
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
        <Label className="text-sm">{name}</Label>
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
        <Label className="text-sm">{name}</Label>
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
      <Label className="text-sm">{name}</Label>
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

  const dead = DEAD_SETTINGS[category] ?? [];
  const entries = Object.entries(localSettings).filter(
    ([key]) => key !== "id" && key !== "category" && !dead.includes(key),
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
    <div className="flex flex-col gap-6 p-6">
      <h1 className="text-2xl font-semibold">Settings</h1>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Appearance</CardTitle>
        </CardHeader>
        <CardContent>
          <ThemeSettings />
        </CardContent>
      </Card>

      {/* Unified folder ordering (cross-account) */}
      <UnifiedOrder />

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
                {key === "spam" && (
                  <div className="flex items-start gap-2 rounded-md border bg-muted/30 p-3 text-sm">
                    <Workflow className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                    <div>
                      Classification and what happens to spam are stages in the{" "}
                      <Link href="/pipeline" className="underline">
                        pipeline
                      </Link>{" "}
                      now -- a <span className="font-mono">classify</span> stage produces the
                      verdict, a <span className="font-mono">match</span> stage decides what
                      happens to it.
                    </div>
                  </div>
                )}
                {allSettings?.[key] ? (
                  <CategorySettings category={key} settings={allSettings[key]} />
                ) : (
                  <div className="py-4 text-sm text-muted-foreground">
                    No settings available for this category
                  </div>
                )}
              </CardContent>
            </Card>
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}
