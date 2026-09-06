/** Which way the reader is moving through the mail list. */

import { atom } from "jotai";

/**
 * The list runs newest-first, so "older" is a step down it and "newer" a
 * step up. Set whenever the reader moves themselves, and read when an
 * action removes the open message: triage carries on the way they were
 * already going rather than jumping to an edge.
 */
export type MailNavDirection = "older" | "newer";

export const mailNavDirectionAtom = atom<MailNavDirection>("older");
