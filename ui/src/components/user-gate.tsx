"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { ApiError, api } from "@/lib/api";
import { bindUser } from "@/lib/user-storage";
import { Button } from "@/components/ui/button";
import type { MeResponse } from "@/types/api";

type Gate =
  | { state: "loading" }
  | { state: "ready"; me: MeResponse }
  | { state: "failed"; message: string };

/**
 * Asks `/api/me` who is signed in, binds that user to browser storage, and
 * only then renders its children. Everything persisted in this browser is
 * namespaced by that id (lib/user-storage.ts), so nothing may read stored
 * state before this has answered. A 401, or a 403 the proxy answers with, is
 * handled in the API client by reloading into the proxy's login; what
 * reaches here is a server that is down or a user the application refuses.
 *
 * The prerender and the first client render both show nothing, so hydration
 * agrees.
 */
export function UserGate({ children }: { children: (me: MeResponse) => ReactNode }) {
  const [gate, setGate] = useState<Gate>({ state: "loading" });

  const load = useCallback(() => {
    setGate({ state: "loading" });
    api.me
      .get()
      .then((me) => {
        if (me.disabled) {
          setGate({ state: "failed", message: "This account has been disabled." });
          return;
        }
        bindUser(me.user_id);
        setGate({ state: "ready", me });
      })
      .catch((err: unknown) => {
        const message =
          err instanceof ApiError && err.status === 403
            ? "You are not allowed to use this instance."
            : "MailVerdict could not reach the server.";
        setGate({ state: "failed", message });
      });
  }, []);

  useEffect(load, [load]);

  if (gate.state === "ready") return <>{children(gate.me)}</>;
  if (gate.state === "failed") {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-sm">
        <p>{gate.message}</p>
        <Button variant="outline" onClick={load}>
          Try again
        </Button>
      </div>
    );
  }
  return null;
}
