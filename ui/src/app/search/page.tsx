"use client";

import { useSearchParams } from "next/navigation";
import { SearchPage } from "@/components/search/search-page";
import { ClientOnly } from "@/components/client-only";

export default function SearchRoute() {
  return (
    <ClientOnly>
      <SearchRouteInner />
    </ClientOnly>
  );
}

/**
 * useSearchParams() calls in here, guarded by ClientOnly above the same
 * way app/page.tsx's own MailView is -- next's static export refuses to
 * prerender it without a Suspense boundary, and nothing renders during
 * the build's prerender pass, so it is never actually called then.
 */
function SearchRouteInner() {
  const searchParams = useSearchParams();
  return <SearchPage initialQuery={searchParams.get("q")} />;
}
