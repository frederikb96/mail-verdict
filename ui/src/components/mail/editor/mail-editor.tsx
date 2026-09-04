"use client";

import { useEffect, useRef } from "react";
import { type Editor, EditorContent, useEditor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import { Markdown } from "@tiptap/markdown";

import { QuotedMessage } from "@/components/mail/editor/quoted-message-node";
import { EditorToolbar } from "@/components/mail/editor/toolbar";
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
}: MailEditorProps) {
  const initialJson = useRef<string | null>(null);
  // handlePaste is captured once by useEditor's own initial options, so a
  // stale closure over `editor` (always undefined at that point, since
  // useEditor has not returned yet) would insert nothing on every paste.
  // A ref sidesteps that without re-creating the editor on every render.
  const editorRef = useRef<Editor | null>(null);

  const editor = useEditor({
    immediatelyRender: false,
    autofocus: autoFocus ? "end" : false,
    extensions: [
      StarterKit.configure({
        link: { openOnClick: false, autolink: true, linkOnPaste: true },
      }),
      Markdown,
      QuotedMessage,
    ],
    content: initialHtml,
    editorProps: {
      attributes: {
        "aria-label": "Message body",
        "data-testid": "mail-editor-body",
        class: "focus:outline-none",
      },
      handlePaste: (_view, event) => {
        const clipboard = event.clipboardData;
        if (!clipboard) return false;
        // HTML wins whenever the clipboard offers it -- ProseMirror's own
        // paste handling already does this correctly, re-parsed through
        // the schema. This hook only covers what that handling cannot: a
        // source (several note apps among them) that puts
        // Markdown-rendered HTML source, as literal text, in the
        // text/plain flavour and offers no text/html at all -- the
        // raw-HTML-as-text failure this editor exists to fix.
        if (clipboard.getData("text/html")) return false;
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
    });
    return () => onReady?.(null);
    // Deliberately keyed on `editor` alone: onReady is an identity the
    // caller is expected to keep stable, and re-running this on every
    // render of theirs would re-announce the same editor repeatedly.
  }, [editor]);

  if (!editor) return null;

  return (
    <div className="mail-editor flex flex-col overflow-hidden rounded-md border">
      <EditorToolbar editor={editor} />
      <div
        data-testid="mail-editor-scroll"
        className={cn(
          "min-h-0 flex-1 overflow-y-auto px-3 py-2",
          compact ? "max-h-[40vh]" : "max-h-[50vh]",
        )}
      >
        <EditorContent editor={editor} className="h-full" />
      </div>
    </div>
  );
}
