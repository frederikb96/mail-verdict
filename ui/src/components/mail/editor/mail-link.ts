/**
 * Link, plus a title attribute mirroring href.
 *
 * A plain `<a href>` only shows its target in the browser's own
 * status-bar preview on hover, which is easy to miss and isn't announced
 * to a screen reader either; a real title attribute gets an accessible
 * native tooltip instead. Editing-only -- the outbound sanitizer's
 * allowlist for "a" is href alone, so this never reaches the recipient,
 * which is fine: their own mail client shows the href on hover regardless
 * of title.
 */

import { Link } from "@tiptap/extension-link";

export const MailLink = Link.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      title: {
        default: null,
        parseHTML: () => null,
        renderHTML: (attributes: Record<string, unknown>) =>
          attributes.href ? { title: attributes.href as string } : {},
      },
    };
  },
});
