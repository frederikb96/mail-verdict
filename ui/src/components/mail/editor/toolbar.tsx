"use client";

import type { Editor } from "@tiptap/react";
import {
  Bold,
  Code,
  Italic,
  Link2,
  List,
  ListChecks,
  ListOrdered,
  Quote,
  RemoveFormatting,
  Strikethrough,
  Underline,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface EditorToolbarProps {
  editor: Editor;
}

function ToolbarButton({
  active,
  onClick,
  title,
  children,
}: {
  active: boolean;
  onClick: () => void;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-xs"
      title={title}
      aria-label={title}
      aria-pressed={active}
      className={cn(active && "bg-muted text-foreground")}
      onClick={onClick}
    >
      {children}
    </Button>
  );
}

/**
 * A small, fixed toolbar over the compose editor.
 *
 * Deliberately minimal -- headings, bold, italic and lists are all
 * reachable by typing Markdown as StarterKit's input rules already do
 * (`# `, `**bold**`, `- `), so the toolbar exists for what typing does
 * not cover well: toggling a mark after the fact, and inserting a link.
 */
export function EditorToolbar({ editor }: EditorToolbarProps) {
  const setLink = () => {
    const previous = editor.getAttributes("link").href as string | undefined;
    // A modal dialog for one field is more machinery than a link insert
    // needs; window.prompt is what most rich-text toolbars, Gmail's own
    // included, use for exactly this.
    const url = window.prompt("Link URL", previous ?? "");
    if (url === null) return;
    if (url === "") {
      editor.chain().focus().extendMarkRange("link").unsetLink().run();
      return;
    }
    editor.chain().focus().extendMarkRange("link").setLink({ href: url }).run();
  };

  return (
    <div className="flex flex-wrap items-center gap-0.5 border-b px-1 py-1">
      <ToolbarButton
        active={editor.isActive("bold")}
        onClick={() => editor.chain().focus().toggleBold().run()}
        title="Bold"
      >
        <Bold />
      </ToolbarButton>
      <ToolbarButton
        active={editor.isActive("italic")}
        onClick={() => editor.chain().focus().toggleItalic().run()}
        title="Italic"
      >
        <Italic />
      </ToolbarButton>
      <ToolbarButton
        active={editor.isActive("underline")}
        onClick={() => editor.chain().focus().toggleUnderline().run()}
        title="Underline"
      >
        <Underline />
      </ToolbarButton>
      <ToolbarButton
        active={editor.isActive("strike")}
        onClick={() => editor.chain().focus().toggleStrike().run()}
        title="Strikethrough"
      >
        <Strikethrough />
      </ToolbarButton>

      <div className="mx-1 h-4 w-px bg-border" />

      <ToolbarButton
        active={editor.isActive("bulletList")}
        onClick={() => editor.chain().focus().toggleBulletList().run()}
        title="Bullet list"
      >
        <List />
      </ToolbarButton>
      <ToolbarButton
        active={editor.isActive("orderedList")}
        onClick={() => editor.chain().focus().toggleOrderedList().run()}
        title="Numbered list"
      >
        <ListOrdered />
      </ToolbarButton>
      <ToolbarButton
        active={editor.isActive("taskList")}
        onClick={() => editor.chain().focus().toggleTaskList().run()}
        title="Checklist"
      >
        <ListChecks />
      </ToolbarButton>
      <ToolbarButton
        active={editor.isActive("blockquote")}
        onClick={() => editor.chain().focus().toggleBlockquote().run()}
        title="Quote"
      >
        <Quote />
      </ToolbarButton>
      <ToolbarButton
        active={editor.isActive("codeBlock")}
        onClick={() => editor.chain().focus().toggleCodeBlock().run()}
        title="Code block"
      >
        <Code />
      </ToolbarButton>

      <div className="mx-1 h-4 w-px bg-border" />

      <ToolbarButton active={editor.isActive("link")} onClick={setLink} title="Link">
        <Link2 />
      </ToolbarButton>
      <ToolbarButton
        active={false}
        onClick={() => editor.chain().focus().unsetAllMarks().clearNodes().run()}
        title="Clear formatting"
      >
        <RemoveFormatting />
      </ToolbarButton>
    </div>
  );
}
