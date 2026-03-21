/** SSE connection state atom. */

import { atom } from "jotai";

export type ConnectionState = "connected" | "reconnecting" | "disconnected";

/** Current SSE connection state, driven by the SSE hook. */
export const sseConnectionStateAtom = atom<ConnectionState>("disconnected");
