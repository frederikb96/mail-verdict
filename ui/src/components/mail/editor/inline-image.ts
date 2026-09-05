/**
 * A pasted image, in the editor and on the wire.
 *
 * The editor shows it via a `blob:` object URL -- the only thing a
 * browser can paint before the message is ever sent -- alongside a
 * `data-cid` attribute carrying a stable id. Nothing here talks to the
 * server: the underlying File stays in memory (see MailEditorHandle's
 * getInlineImages()) until the message is actually submitted, at which
 * point compose-form.tsx rewrites every `data-cid` image's `src` to
 * `cid:<value>` (rewriteInlineImageSrcs, below) and uploads the matching
 * File as an inline attachment -- the same `cid:` mechanism PostIMAP's
 * outbox_attachments.content_id resolves against, never an image encoded
 * into the HTML itself, which several major mail clients refuse to
 * render.
 */

import { Image } from "@tiptap/extension-image";

/** Image, extended with a stable content id and in-editor resize handles.
 * Resizing writes width/height node attributes (onCommit below), which
 * render as plain width/height HTML attributes -- not a style declaration
 * the outbound sanitizer would strip. */
export const InlineImage = Image.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      "data-cid": {
        default: null,
        parseHTML: (element: HTMLElement) => element.getAttribute("data-cid"),
      },
    };
  },
  // The base Image extension's own renderMarkdown writes the live src
  // (a blob: object URL while composing) straight into the markdown
  // body_text -- meaningless, and worse, a local browser-session URL
  // leaking into a message that leaves this tab. Point it at the same
  // cid: reference the HTML rewrite (rewriteInlineImageSrcs, in
  // compose-form.tsx) already gives the recipient's client to resolve.
  renderMarkdown(node: { attrs?: Record<string, unknown> }) {
    const contentId = node.attrs?.["data-cid"];
    const alt = (node.attrs?.alt as string | undefined) ?? "";
    return contentId ? `![${alt}](cid:${contentId})` : `![${alt}]()`;
  },
}).configure({
  inline: true,
  resize: { enabled: true, minWidth: 40, minHeight: 40 },
});

/** One pasted image, tracked outside the editor's own document so its
 * File survives independently of however many times the node itself is
 * moved or its attributes rewritten. */
export interface InlineImageEntry {
  contentId: string;
  file: File;
  blobUrl: string;
}

let inlineImageCounter = 0;

/** A per-message-unique id -- crypto.randomUUID() would do as well, but
 * every other id-like value in this editor module is already a small
 * incrementing counter (see quoted-message-node.ts's own conventions),
 * and a content id only ever needs to be unique within one message. */
function nextContentId(): string {
  inlineImageCounter += 1;
  return `img${Date.now()}${inlineImageCounter}`;
}

/** Register a pasted or dropped image file for insertion -- the blob URL
 * is what the editor renders; the File is what compose-form.tsx uploads
 * if the image is still referenced when the message is sent. */
export function registerInlineImage(file: File): InlineImageEntry {
  return { contentId: nextContentId(), file, blobUrl: URL.createObjectURL(file) };
}

const _DATA_URI_RE = /^data:([^;,]+)?(;base64)?,([\s\S]*)$/;

/** Decode a `data:` URI into a File, synchronously -- no fetch() needed
 * for a URI the browser already resolved locally. Used for HTML paste
 * sources (some editors and PDF viewers embed a copied image as base64
 * directly in the clipboard's HTML flavour) rather than offering a
 * separate image clipboard item the way a screenshot tool does. */
export function dataUriToFile(dataUri: string, filename: string): File | null {
  const match = _DATA_URI_RE.exec(dataUri);
  if (!match) return null;
  const mime = match[1] || "application/octet-stream";
  const isBase64 = Boolean(match[2]);
  const payload = match[3];
  if (!mime.startsWith("image/")) return null;
  try {
    const bytes = isBase64
      ? Uint8Array.from(atob(payload), (c) => c.charCodeAt(0))
      : new TextEncoder().encode(decodeURIComponent(payload));
    return new File([bytes], filename, { type: mime });
  } catch {
    return null;
  }
}

/** Every data-cid an editor's serialised HTML still actually references --
 * an image deleted from the document (row: deleting a pasted image drops
 * its attachment too) is simply absent here, which is what lets the
 * caller stop uploading a File nothing in the message points at anymore. */
export function extractReferencedContentIds(html: string): Set<string> {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const ids = new Set<string>();
  doc.querySelectorAll("img[data-cid]").forEach((img) => {
    const id = img.getAttribute("data-cid");
    if (id) ids.add(id);
  });
  return ids;
}

/**
 * Convert every `<img src="data:...">` in a pasted HTML fragment into an
 * inline image the same way a directly pasted image file becomes one --
 * a `blob:` src plus a `data-cid` attribute -- rather than leaving the
 * base64 payload in the document. Some sources (a few note apps and PDF
 * viewers among them) embed a copied image as base64 directly in the
 * clipboard's HTML flavour instead of offering a separate image
 * clipboard item; without this, the default HTML parse would either drop
 * the tag (data: URIs are rejected by the image schema by default) or
 * carry the base64 payload straight through to the outbox.
 */
export function convertDataUriImages(html: string): {
  html: string;
  entries: InlineImageEntry[];
} {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const entries: InlineImageEntry[] = [];
  let index = 0;
  doc.querySelectorAll('img[src^="data:"]').forEach((img) => {
    const src = img.getAttribute("src");
    if (!src) return;
    index += 1;
    const file = dataUriToFile(src, `pasted-image-${index}`);
    if (!file) {
      img.remove();
      return;
    }
    const entry = registerInlineImage(file);
    entries.push(entry);
    img.setAttribute("src", entry.blobUrl);
    img.setAttribute("data-cid", entry.contentId);
  });
  return { html: doc.body.innerHTML, entries };
}

/** Rewrite every inline image's display-only `blob:` src to the
 * `cid:<value>` reference the recipient's mail client actually resolves
 * -- the last step before this HTML reaches the outbox API. */
export function rewriteInlineImageSrcs(html: string): string {
  const doc = new DOMParser().parseFromString(html, "text/html");
  doc.querySelectorAll("img[data-cid]").forEach((img) => {
    const id = img.getAttribute("data-cid");
    if (id) img.setAttribute("src", `cid:${id}`);
  });
  return doc.body.innerHTML;
}
