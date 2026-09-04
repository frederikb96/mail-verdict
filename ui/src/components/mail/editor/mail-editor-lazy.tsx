"use client";

import dynamic from "next/dynamic";

import { Skeleton } from "@/components/ui/skeleton";

/**
 * The editor and its Tiptap dependencies (~140 KB gzipped) load only once
 * a composer actually opens, so the mail list's first paint never pays
 * for them. ssr:false because the static export has no server to render
 * against; useEditor's own immediatelyRender:false guard is for a
 * different problem (a client-side hydration mismatch), not this one.
 *
 * MailEditor hands its imperative API up through an onReady callback
 * prop rather than a ref -- the loader next/dynamic wraps a component in
 * here does not forward refs, only ordinary props.
 */
export const MailEditorLazy = dynamic(
  () => import("@/components/mail/editor/mail-editor").then((m) => m.MailEditor),
  {
    ssr: false,
    loading: () => <Skeleton className="h-32 w-full rounded-md" />,
  },
);
