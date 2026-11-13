/** Who is signed in: the user and role from GET /api/me. */

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { boundUser } from "@/lib/user-storage";
import type { MeResponse } from "@/types/api";

export const meKey = ["me"] as const;

/** Re-asking is how a session replaced in another tab is noticed. */
const ME_STALE_MS = 60_000;

/**
 * The signed-in user. `UserGate` has already answered `/api/me` before
 * anything below it renders, so this is never undefined; it stays a query so
 * a refetch (window focus) can notice that someone else is signed in now,
 * which reloads the page rather than showing one person's screen over
 * another's state.
 */
export function useMe(): MeResponse {
  const { data } = useQuery({
    queryKey: meKey,
    queryFn: () => api.me.get(),
    staleTime: ME_STALE_MS,
    // Mounted by many components; the focus refetch is the one that matters.
    refetchOnMount: false,
  });

  useEffect(() => {
    const bound = boundUser();
    if (data && bound !== null && data.user_id !== bound) window.location.reload();
  }, [data]);

  if (!data) throw new Error("useMe was used outside the signed-in user's gate");
  return data;
}

export function useIsAdmin(): boolean {
  return useMe().role === "admin";
}
