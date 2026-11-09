"use client";

import { useEffect } from "react";
import { attachKeyboardSafeViewport } from "@/lib/keyboard-safe-viewport";

/** Publishes the visible height as `--app-vh` and undoes Safari's keyboard pan; the app shell
 *  (`SidebarProvider`'s wrapper) is as tall as that variable. */
export function KeyboardSafeViewport() {
  useEffect(() => attachKeyboardSafeViewport(window, document.documentElement), []);
  return null;
}
