"use client";

import { useEffect, useRef } from "react";
import { type Editor, EditorContent, useEditor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import { Markdown } from "@tiptap/markdown";
import { TableKit } from "@tiptap/extension-table";
import { TaskList } from "@tiptap/extension-task-list";
import { TaskItem } from "@tiptap/extension-task-item";

import { QuotedMessage } from "@/components/mail/editor/quoted-message-node";
import { EditorToolbar } from "@/components/mail/editor/toolbar";
import { MailLink } from "@/components/mail/editor/mail-link";
import { CutLine } from "@/components/mail/editor/cut-line";
import {
  InlineImage,
  type InlineImageEntry,
  convertDataUriImages,
  extractReferencedContentIds,
  registerInlineImage,
} from "@/components/mail/editor/inline-image";
import { cn } from "@/lib/utils";

export interface MailEditorHandle {
  /** Sanitised on the server before it is ever sent -- see
   * core/outbound_sanitizer.py -- so this is the editor's raw output,
   * not something safe to trust on its own. */
  getHTML: () => string;
  /** The authored part only; a quoted or forwarded message renders as
   * nothing here (see quoted-message-node.ts's renderMarkdown), since the
   * `> `-prefixed plain-text form of the original is built separately
   * from its own body_text and appended by the caller. */
  getMarkdown: () => string;
  isEmpty: () => boolean;
  focus: () => void;
  /** Every pasted image still referenced by the current document, each
   * with the File compose-form.tsx uploads as an inline attachment on
   * submit -- an image deleted from the document since it was pasted is
   * simply absent here, which is what makes deleting it also drop the
   * attachment it would otherwise have carried. */
  getInlineImages: () => InlineImageEntry[];
}

interface MailEditorProps {
  /** Initial content, as HTML -- either empty, a bare paragraph plus an
   * embedded quote (see quoted-message-node.ts's parseHTML), or a whole
   * previously-saved draft body. */
  initialHtml?: string;
  autoFocus?: boolean;
  /** Compact (reply box) vs a full dialog -- only the available height
   * differs; everything else about the editor is the same surface. */
  compact?: boolean;
  onDirtyChange?: (dirty: boolean) => void;
  /** Called once the editor exists, and again with null on unmount.
   *
   * A ref would be the ordinary way to hand this up, but this component
   * is loaded through next/dynamic (mail-editor-lazy.tsx) so its first
   * paint is deferred out of the mail list's own bundle -- and the
   * loader next/dynamic wraps components in does not forward refs, only
   * ordinary props. A callback prop sidesteps that rather than fighting it.
   */
  onReady?: (handle: MailEditorHandle | null) => void;
  /** Overrides the compact/default max-height cap below with a specific
   * pixel value -- the resizable panel's drag handle (compose-form.tsx)
   * grows or shrinks this rather than the whole surrounding form. */
  heightPx?: number;
  /** Fills its container's height instead of capping at a max-height --
   * the maximized state of the resizable panel above. */
  fillHeight?: boolean;
}

/**
 * A markdown-aware, mail-safe rich text editor sharing one surface across
 * the compose dialog, the reply box and the draft editor.
 */
export function MailEditor({
  initialHtml = "",
  autoFocus = false,
  compact = false,
  onDirtyChange,
  onReady,
  heightPx,
  fillHeight = false,
}: MailEditorProps) {
  const initialJson = useRef<string | null>(null);
  // handlePaste is captured once by useEditor's own initial options, so a
  // stale closure over `editor` (always undefined at that point, since
  // useEditor has not returned yet) would insert nothing on every paste.
  // A ref sidesteps that without re-creating the editor on every render.
  const editorRef = useRef<Editor | null>(null);
  // Every pasted image's File, keyed by the content id its node carries --
  // tracked here rather than in the editor's own document, so the File
  // survives independently of however the node is later moved, resized
  // or removed. getInlineImages() below is what makes "removed from the
  // document" and "no longer uploaded" the same predicate.
  const inlineImagesRef = useRef<Map<string, InlineImageEntry>>(new Map());

  useEffect(
    () => () => {
      // Object URLs are not reclaimed by garbage collection -- only an
      // explicit revoke releases the underlying blob, and the compose
      // surface unmounting (sent, discarded, or the dialog closed) is the
      // only point this editor instance is ever done with them.
      for (const entry of inlineImagesRef.current.values()) {
        URL.revokeObjectURL(entry.blobUrl);
      }
    },
    [],
  );

  const editor = useEditor({
    immediatelyRender: false,
    autofocus: autoFocus ? "end" : false,
    extensions: [
      StarterKit.configure({
        link: false,
      }),
      MailLink.configure({ openOnClick: false, autolink: true, linkOnPaste: true }),
      Markdown,
      QuotedMessage,
      TableKit.configure({
        // Column-drag resizing writes inline widths the outbound
        // sanitizer strips anyway (no style survives on td/th), so it is
        // switched off rather than producing a table that renders
        // differently in the editor than in the sent message.
        table: { resizable: false },
      }),
      TaskList,
      TaskItem.configure({ nested: true }),
      InlineImage,
      CutLine,
    ],
    content: initialHtml,
    editorProps: {
      attributes: {
        "aria-label": "Message body",
        "data-testid": "mail-editor-body",
        class: "focus:outline-none",
      },
      // Ctrl/Cmd-click opens a link's target in a new tab, the ordinary
      // rich-text-editor convention -- a plain click keeps placing the
      // cursor (openOnClick above is off for exactly that reason).
      handleClick: (_view, _pos, event) => {
        if (!(event.ctrlKey || event.metaKey)) return false;
        const target = event.target as HTMLElement | null;
        const link = target?.closest("a");
        if (!link?.href) return false;
        window.open(link.href, "_blank", "noopener,noreferrer");
        return true;
      },
      handlePaste: (_view, event) => {
        const clipboard = event.clipboardData;
        if (!clipboard) return false;

        // An image on the clipboard (a screenshot tool, or a file
        // manager's own copy) arrives as a file, not as an HTML or plain
        // text flavour -- checked first since a source offering both an
        // image file and some HTML alternative should still paste as the
        // image.
        const imageFile = Array.from(clipboard.files ?? []).find((f) =>
          f.type.startsWith("image/"),
        );
        if (imageFile) {
          const entry = registerInlineImage(imageFile);
          inlineImagesRef.current.set(entry.contentId, entry);
          editorRef.current
            ?.chain()
            .focus()
            .insertContent({
              type: "image",
              attrs: { src: entry.blobUrl, alt: imageFile.name, "data-cid": entry.contentId },
            })
            .run();
          return true;
        }

        // HTML wins whenever the clipboard offers it -- ProseMirror's own
        // paste handling already does this correctly, re-parsed through
        // the schema, tables and checklists included. This hook only
        // covers what that handling cannot: a source (several note apps
        // among them) that puts Markdown-rendered HTML source, as literal
        // text, in the text/plain flavour and offers no text/html at all
        // -- the raw-HTML-as-text failure this editor exists to fix -- and
        // an embedded base64 image, which convertDataUriImages turns into
        // the same blob-plus-content-id shape a directly pasted image
        // file gets, rather than leaving the payload in the document.
        const html = clipboard.getData("text/html");
        if (html) {
          if (!/<img\b[^>]*\bsrc=["']data:/i.test(html)) return false;
          const { html: converted, entries } = convertDataUriImages(html);
          for (const entry of entries) inlineImagesRef.current.set(entry.contentId, entry);
          editorRef.current?.chain().focus().insertContent(converted).run();
          return true;
        }
        const text = clipboard.getData("text/plain");
        if (!text) return false;
        editorRef.current?.chain().focus().insertContent(text, { contentType: "markdown" }).run();
        return true;
      },
    },
    onUpdate: ({ editor: current }) => {
      if (initialJson.current === null) return;
      onDirtyChange?.(JSON.stringify(current.getJSON()) !== initialJson.current);
    },
    onCreate: ({ editor: current }) => {
      initialJson.current = JSON.stringify(current.getJSON());
    },
  });

  editorRef.current = editor;

  useEffect(() => {
    if (!editor) return;
    onReady?.({
      getHTML: () => editor.getHTML(),
      getMarkdown: () => editor.getMarkdown(),
      isEmpty: () => editor.isEmpty,
      focus: () => {
        editor.chain().focus().run();
      },
      getInlineImages: () => {
        const referenced = extractReferencedContentIds(editor.getHTML());
        return Array.from(inlineImagesRef.current.values()).filter((entry) =>
          referenced.has(entry.contentId),
        );
      },
    });
    return () => onReady?.(null);
    // Deliberately keyed on `editor` alone: onReady is an identity the
    // caller is expected to keep stable, and re-running this on every
    // render of theirs would re-announce the same editor repeatedly.
  }, [editor]);

  if (!editor) return null;

  return (
    <div className="mail-editor flex flex-1 flex-col overflow-hidden rounded-md border">
      <EditorToolbar editor={editor} />
      <div
        data-testid="mail-editor-scroll"
        className={cn(
          "min-h-0 overflow-y-auto px-3 py-2",
          // flex-1 (flex-grow with a 0 basis) is what fillHeight actually
          // needs to reach its container's height -- with an explicit
          // pixel height instead, flex-basis:0 would still win the
          // layout and collapse this back to content size, the same way
          // it did before this was a fixed height rather than a cap.
          fillHeight
            ? "flex-1"
            : heightPx
              ? "shrink-0"
              : (compact ? "max-h-[40vh]" : "max-h-[50vh]") + " flex-1",
        )}
        // A fixed height, not a cap -- the drag handle is a resizable
        // panel, not a ceiling content only reaches once it overflows.
        // Dragging it taller has to grow the visible editing surface
        // immediately, empty or not, the same as dragging any other
        // resizable box.
        style={!fillHeight && heightPx ? { height: heightPx } : undefined}
      >
        <EditorContent editor={editor} className="h-full" />
      </div>
    </div>
  );
}
