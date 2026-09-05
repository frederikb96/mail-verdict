/**
 * Ctrl/Cmd+X with nothing selected cuts the whole current line -- with a
 * selection it falls through to the browser's own cut, unchanged.
 *
 * No keyboard-shortcut layer exists inside this editor otherwise -- the
 * application's own shortcuts are switched off while an editable element
 * has focus (see use-keyboard-shortcuts.ts), so this is the first Tiptap
 * extension in this codebase adding one of its own; QuotedMessage is the
 * precedent for a small, self-contained custom extension living beside
 * the editor rather than as a toolbar concern.
 */

import { Extension } from "@tiptap/core";

export const CutLine = Extension.create({
  name: "cutLine",

  addKeyboardShortcuts() {
    return {
      "Mod-x": () => {
        const { state, view } = this.editor;
        const { selection } = state;
        // A real selection falls through to the browser's native cut --
        // returning false here means this shortcut never ran at all.
        if (!selection.empty) return false;

        const { $from } = selection;
        const depth = $from.depth;
        // At the document root there is no enclosing block to cut.
        if (depth === 0) return false;

        const from = $from.start(depth);
        const to = $from.end(depth);
        const text = state.doc.textBetween(from, to, "\n");

        if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
          navigator.clipboard.writeText(text).catch(() => {
            // Clipboard access can be denied (permissions, insecure
            // context); the cut itself still proceeds either way, the
            // same as every other editor when only the clipboard write
            // fails.
          });
        }

        // Every schema node here requires at least one child in its
        // parent, so the sole remaining line in a list item or the
        // document itself can't be removed outright -- it is cleared
        // instead, the same empty-line result Backspace already leaves.
        const parent = $from.node(depth - 1);
        if (parent.childCount <= 1) {
          view.dispatch(state.tr.delete(from, to));
          return true;
        }

        view.dispatch(state.tr.delete($from.before(depth), $from.after(depth)));
        return true;
      },
    };
  },
});
