"use client";

import type { ReactNode } from "react";
import { ShieldAlert } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { useIsAdmin } from "@/hooks/use-me";

/** Renders its children for administrators and an explanation for everyone
 * else -- never a blank page, so a link someone shared still says why it
 * shows nothing. The API refuses admin requests from users regardless. */
export function AdminOnly({ children }: { children: ReactNode }) {
  const isAdmin = useIsAdmin();
  if (isAdmin) return <>{children}</>;
  return (
    <div className="p-6">
      <Card>
        <CardContent className="flex items-center gap-3 pt-6 text-sm">
          <ShieldAlert className="h-5 w-5 shrink-0 text-muted-foreground" />
          <div>
            <div className="font-medium">Administrators only</div>
            <div className="text-muted-foreground">
              This part of MailVerdict is managed by the administrator of this instance.
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

/** Renders its children for administrators only, nothing otherwise -- for a
 * control or section inside a page everyone can open. */
export function AdminSection({ children }: { children: ReactNode }) {
  return useIsAdmin() ? <>{children}</> : null;
}
