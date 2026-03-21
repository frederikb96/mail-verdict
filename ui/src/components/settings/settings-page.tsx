"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Save,
  Loader2,
  Bot,
  ShieldAlert,
  RefreshCw,
  Repeat,
  FileCode,
  Sun,
  Moon,
  Monitor,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";

import { useAllSettings, useUpdateSettings } from "@/hooks/use-settings";
import { useTheme } from "@/components/theme-provider";

const CATEGORIES = [
  { key: "ai", label: "AI", icon: Bot },
  { key: "spam", label: "Spam", icon: ShieldAlert },
  { key: "sync", label: "Sync", icon: RefreshCw },
  { key: "retry", label: "Retry", icon: Repeat },
  { key: "rules", label: "Rules", icon: FileCode },
] as const;

/** Locked fields that cannot be edited from the UI (managed by environment). */
const LOCKED_FIELDS = new Set(["api_key"]);

function SettingField({
  name,
  value,
  onChange,
}: {
  name: string;
  value: unknown;
  onChange: (name: string, value: unknown) => void;
}) {
  const isLocked = LOCKED_FIELDS.has(name);

  if (typeof value === "boolean") {
    return (
      <div className="flex items-center justify-between">
        <Label className="text-sm">{name}</Label>
        <input
          type="checkbox"
          checked={value}
          disabled={isLocked}
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
          disabled={isLocked}
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
          disabled={isLocked}
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
  const isPassword = name.toLowerCase().includes("key") ||
    name.toLowerCase().includes("password") ||
    name.toLowerCase().includes("secret");

  return (
    <div className="grid gap-1.5">
      <div className="flex items-center gap-2">
        <Label className="text-sm">{name}</Label>
        {isLocked && (
          <Badge variant="outline" className="text-xs">
            locked
          </Badge>
        )}
      </div>
      <Input
        type={isPassword ? "password" : "text"}
        value={String(value ?? "")}
        disabled={isLocked}
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

  const entries = Object.entries(localSettings).filter(
    ([key]) => key !== "id" && key !== "category",
  );

  return (
    <div className="flex flex-col gap-4">
      {entries.map(([key, value]) => (
        <SettingField
          key={key}
          name={key}
          value={value}
          onChange={handleChange}
        />
      ))}
      {entries.length === 0 && (
        <div className="py-4 text-sm text-muted-foreground">
          No settings in this category
        </div>
      )}
      {dirty && (
        <div className="flex justify-end">
          <Button
            onClick={handleSave}
            disabled={updateSettings.isPending}
          >
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
              <CardContent className="pt-6">
                {allSettings?.[key] ? (
                  <CategorySettings
                    category={key}
                    settings={allSettings[key]}
                  />
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
