"use client";

import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";

interface ErrorFallbackProps {
  section?: string;
  error?: Error;
  onReset?: () => void;
}

/** Reusable fallback UI shown when an error boundary catches an error. */
export function ErrorFallback({ section, error, onReset }: ErrorFallbackProps) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 p-6 text-muted-foreground">
      <AlertTriangle className="h-8 w-8 text-destructive opacity-70" />
      <p className="text-sm font-medium">
        Something went wrong{section ? ` in ${section}` : ""}
      </p>
      {error?.message && (
        <p className="max-w-md text-center text-xs opacity-70">
          {error.message}
        </p>
      )}
      {onReset && (
        <Button variant="outline" size="sm" onClick={onReset}>
          Try Again
        </Button>
      )}
    </div>
  );
}
