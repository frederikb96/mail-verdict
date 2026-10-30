/** Adding a file to the register from the browser: what the picker offers,
 * the HEIC question, and waiting for the file job to place the file. */

import type { OrderFileStatus } from "@/types/api";

/** The file types the server accepts (`order_files.accepted_types`). HEIC is
 * not among them -- see `toUploadable`. */
export const ACCEPTED_UPLOADS =
  ".pdf,.png,.jpg,.jpeg,.pkpass,.ics,.txt,.html,.xml,image/png,image/jpeg";

type Named = { name: string; type: string };

export function isHeic(file: Named): boolean {
  return /^image\/hei[cf]$/i.test(file.type) || /\.hei[cf]$/i.test(file.name);
}

export function jpegName(name: string): string {
  const dot = name.lastIndexOf(".");
  return `${dot > 0 ? name.slice(0, dot) : name}.jpg`;
}

/** The file to send. A HEIC photo is redrawn as a JPEG when this browser
 * can decode HEIC (Safari can), since the server refuses it with a 415;
 * otherwise the person is told why. Anything else goes through untouched. */
export async function toUploadable(file: File): Promise<File> {
  if (!isHeic(file)) return file;
  try {
    const bitmap = await createImageBitmap(file);
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    canvas.getContext("2d")?.drawImage(bitmap, 0, 0);
    bitmap.close();
    const blob = await new Promise<Blob | null>((resolve) =>
      canvas.toBlob(resolve, "image/jpeg", 0.92),
    );
    if (blob) return new File([blob], jpegName(file.name), { type: "image/jpeg" });
  } catch {
    // This browser cannot decode HEIC.
  }
  throw new Error(
    `${file.name} is a HEIC photo, which can't be added. Export it as JPEG and try again.`,
  );
}

export interface WaitOptions {
  intervalMs: number;
  timeoutMs: number;
  sleep: (ms: number) => Promise<void>;
}

/** Asks for the file's status until it is placed or has failed. A file still
 * pending at the timeout is handed back pending: the entry announces itself
 * when the job finishes, so nothing is lost by giving up the wait. */
export async function waitForFile(
  fetchStatus: () => Promise<OrderFileStatus>,
  options: WaitOptions,
): Promise<OrderFileStatus> {
  let elapsed = 0;
  for (;;) {
    const status = await fetchStatus();
    if (status.status !== "pending") return status;
    if (elapsed + options.intervalMs > options.timeoutMs) return status;
    await options.sleep(options.intervalMs);
    elapsed += options.intervalMs;
  }
}
