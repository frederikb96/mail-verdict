"use client";

import { Monitor, Moon, Palette, Sun } from "lucide-react";

import { Button } from "@/components/ui/button";
import { CategoryHeader, SectionCard } from "@/components/settings/blocks";
import { useTheme } from "@/components/theme-provider";

const THEMES = [
  { value: "light" as const, icon: Sun, label: "Light" },
  { value: "dark" as const, icon: Moon, label: "Dark" },
  { value: "system" as const, icon: Monitor, label: "System" },
];

export function AppearanceSettings() {
  const { theme, setTheme } = useTheme();
  return (
    <>
      <CategoryHeader title="Appearance" />
      <SectionCard icon={Palette} title="Theme" description="System follows the setting of your device.">
        <div className="flex flex-wrap gap-2" role="group" aria-label="Theme">
          {THEMES.map(({ value, icon: Icon, label }) => (
            <Button
              key={value}
              variant={theme === value ? "default" : "outline"}
              size="sm"
              aria-pressed={theme === value}
              onClick={() => setTheme(value)}
            >
              <Icon className="mr-1 h-3.5 w-3.5" />
              {label}
            </Button>
          ))}
        </div>
      </SectionCard>
    </>
  );
}
