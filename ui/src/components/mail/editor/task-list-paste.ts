/** Checklists pasted from a web page.
 *
 * TaskList and TaskItem only recognise their own markup -- `ul` and `li`
 * carrying `data-type="taskList"`/`"taskItem"`. Everywhere else on the web a
 * rendered checklist is an ordinary `li` with a disabled `input[type=checkbox]`
 * in front of the text, which is what a browser puts on the clipboard when
 * someone copies one. Without a rule for that shape the checkboxes are dropped
 * and the paste lands as a plain bullet list.
 *
 * The rules sit at a higher priority than bulletList/listItem so that a list
 * whose items carry checkboxes wins over the ordinary list parse, and fall
 * through to it for every list that does not.
 */

import { TaskItem } from "@tiptap/extension-task-item";
import { TaskList } from "@tiptap/extension-task-list";

const PASTED_LIST_PRIORITY = 51;

function hasCheckbox(element: HTMLElement, selector: string): boolean {
  return element.querySelector(selector) !== null;
}

export const PastedTaskList = TaskList.extend({
  parseHTML() {
    return [
      ...(this.parent?.() ?? []),
      {
        tag: "ul",
        priority: PASTED_LIST_PRIORITY,
        getAttrs: (node) =>
          hasCheckbox(node as HTMLElement, "li input[type='checkbox']") ? {} : false,
      },
    ];
  },
});

export const PastedTaskItem = TaskItem.extend({
  // The `checked` attribute reads `data-checked` off the list item, which is
  // this editor's own markup and is absent from everyone else's -- so without
  // this the rule below matches the item and every box comes back unticked.
  addAttributes() {
    return {
      ...this.parent?.(),
      checked: {
        default: false,
        keepOnSplit: false,
        parseHTML: (element: HTMLElement) => {
          const own = element.getAttribute("data-checked");
          if (own !== null) return own === "" || own === "true";
          const box = element.querySelector("input[type='checkbox']");
          return box instanceof HTMLInputElement && box.checked;
        },
        renderHTML: (attributes: Record<string, unknown>) => ({
          "data-checked": attributes.checked,
        }),
      },
    };
  },

  parseHTML() {
    return [
      ...(this.parent?.() ?? []),
      {
        tag: "li",
        priority: PASTED_LIST_PRIORITY,
        // Only whether this is a task item at all -- its ticked state comes
        // from the `checked` attribute above, so the two never disagree.
        getAttrs: (node) =>
          hasCheckbox(node as HTMLElement, "input[type='checkbox']") ? {} : false,
      },
    ];
  },
});
