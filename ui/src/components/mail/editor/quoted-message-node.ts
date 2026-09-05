/**
 * A reply or forward's quoted original, embedded in the editor's document
 * as a single atom node rather than parsed into the editor's own schema.
 *
 * The schema Tiptap builds the rest of the editor on is deliberately
 * small -- see mail-editor.tsx -- so parsing an arbitrary quoted message
 * into it would flatten anything it doesn't have a node for (a table
 * becomes a paragraph, styling is dropped). renderHTML below returns a
 * real DOM element with the quote's own markup as its innerHTML instead
 * of a schema-derived structure, so editor.getHTML() emits the fragment
 * verbatim; parseHTML matches the same shape back on reopening a draft.
 */

import { Node } from "@tiptap/core";

import { getEmailStyles } from "@/components/mail/email-renderer";

export interface QuotedMessageAttributes {
  /** Sanitised HTML of the quoted/forwarded message body. */
  html: string;
  /** The attribution line shown above it -- "On <date>, <name> wrote:"
   * for a reply, or the forwarded-message header block for a forward. */
  attribution: string;
}

declare module "@tiptap/core" {
  interface Commands<ReturnType> {
    quotedMessage: {
      insertQuotedMessage: (attrs: QuotedMessageAttributes) => ReturnType;
    };
  }
}

const QUOTE_BAR_STYLE = "margin:0 0 0 .8ex;border-left:1px #ccc solid;padding-left:1ex";

/** Append a possibly multi-line attribution as text nodes joined by real
 * <br> elements -- a forward's header block is several lines, and this is
 * markup that leaves the app for other mail clients, so it cannot rely on
 * a `white-space` declaration surviving whatever the outbound sanitiser
 * or the recipient's own client does to it. */
function appendMultilineText(target: HTMLElement, text: string): void {
  const lines = text.split("\n");
  lines.forEach((line, index) => {
    if (index > 0) target.append(document.createElement("br"));
    target.append(document.createTextNode(line));
  });
}

/** The inverse of appendMultilineText -- `.textContent` alone would lose
 * every line break, since a <br> element contributes nothing to it. */
function extractMultilineText(target: HTMLElement): string {
  let text = "";
  for (const child of Array.from(target.childNodes)) {
    // nodeType 3 is a DOM text node -- the global Node constant it is
    // ordinarily read from is shadowed in this file by Tiptap's own
    // Node (the class node types are defined with), which carries no
    // such constant.
    if (child.nodeType === 3) text += child.textContent ?? "";
    else if (child.nodeName === "BR") text += "\n";
  }
  return text;
}

/** The DOM shape renderHTML below produces, factored out so a caller
 * building an editor's *initial* content (compose-form.tsx, assembling a
 * reply or forward before the editor exists to run any command) can
 * serialise the exact same markup parseHTML expects back, rather than a
 * second hand-written approximation of it. */
export function buildQuotedMessageElement(attrs: QuotedMessageAttributes): HTMLDivElement {
  const wrapper = document.createElement("div");
  wrapper.setAttribute("data-quoted-message", "true");
  wrapper.className = "gmail_quote";

  const attribution = document.createElement("div");
  attribution.className = "gmail_attr";
  appendMultilineText(attribution, attrs.attribution);

  const blockquote = document.createElement("blockquote");
  blockquote.setAttribute("type", "cite");
  blockquote.setAttribute("class", "gmail_quote");
  blockquote.setAttribute("style", QUOTE_BAR_STYLE);
  blockquote.innerHTML = attrs.html;

  wrapper.append(attribution, blockquote);
  return wrapper;
}

/** The same markup as buildQuotedMessageElement, serialised -- for
 * building an editor's initial `content` string outside any editor
 * instance. */
export function buildQuotedMessageHtml(attrs: QuotedMessageAttributes): string {
  return buildQuotedMessageElement(attrs).outerHTML;
}

/** The inverse of buildQuotedMessageElement -- pulled out so parseHTML
 * below (reopening a draft inside the editor) and parseQuotedMessageAttrs
 * (reading one outside it, before the editor exists -- see
 * draft-editor.tsx) share one reconstruction rather than two that could
 * drift apart. */
function readQuotedMessageAttrs(wrapper: HTMLElement): QuotedMessageAttributes {
  const blockquote = wrapper.querySelector("blockquote");
  const attribution = wrapper.querySelector(".gmail_attr");
  return {
    html: blockquote ? blockquote.innerHTML : "",
    attribution: attribution instanceof HTMLElement ? extractMultilineText(attribution) : "",
  };
}

/** Find and read a quoted-message wrapper inside an arbitrary HTML
 * string, outside of any editor instance -- a reopened draft's own quote,
 * before ComposeForm has parsed it into the editor's document. Returns
 * null when the string carries no such wrapper (a fresh compose, or a
 * draft with no quote in it). */
export function parseQuotedMessageAttrs(html: string): QuotedMessageAttributes | null {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const wrapper = doc.querySelector('div[data-quoted-message="true"]');
  return wrapper instanceof HTMLElement ? readQuotedMessageAttrs(wrapper) : null;
}

export const QuotedMessage = Node.create({
  name: "quotedMessage",
  group: "block",
  atom: true,
  selectable: true,
  draggable: false,
  isolating: true,
  // No markdown authoring surface exists for this node -- it only ever
  // arrives through insertQuotedMessage -- and the > -prefixed plain-text
  // form is built separately from the original message's body_text, so
  // it renders as nothing in the markdown alternative rather than being
  // walked as ordinary content.
  renderMarkdown: () => "",

  addAttributes() {
    return {
      html: { default: "" },
      attribution: { default: "" },
    };
  },

  parseHTML() {
    return [
      {
        tag: 'div[data-quoted-message="true"]',
        // renderHTML below builds the attribution and the quoted body as
        // plain markup, not as attributes on the wrapper -- so reopening
        // a saved HTML draft has to read them back out of that markup
        // the same way it wrote them, rather than from an attribute the
        // wrapper never carried.
        getAttrs: (dom) => (dom instanceof HTMLElement ? readQuotedMessageAttrs(dom) : false),
      },
    ];
  },

  renderHTML({ node }) {
    return buildQuotedMessageElement(node.attrs as QuotedMessageAttributes);
  },

  addCommands() {
    return {
      insertQuotedMessage:
        (attrs: QuotedMessageAttributes) =>
        ({ commands }) =>
          commands.insertContent({ type: this.name, attrs }),
    };
  },

  addNodeView() {
    return ({ node }) => {
      const dom = document.createElement("div");
      dom.className = "quoted-message-node";
      dom.contentEditable = "false";

      const attribution = document.createElement("div");
      attribution.className = "quoted-message-attribution";
      attribution.textContent = node.attrs.attribution;

      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "quoted-message-toggle";

      const host = document.createElement("div");
      host.setAttribute("data-testid", "quoted-message-shadow-host");
      const shadow = host.attachShadow({ mode: "open" });
      const style = document.createElement("style");
      // Quoted mail is written assuming a light canvas, same as the
      // reading pane -- see email-renderer.tsx for why that holds even
      // when the app itself is in dark mode.
      style.textContent = getEmailStyles("light");
      const content = document.createElement("div");
      content.innerHTML = node.attrs.html;
      shadow.append(style, content);

      let collapsed = true;
      const applyCollapsed = () => {
        host.style.display = collapsed ? "none" : "block";
        toggle.textContent = collapsed ? "Show quoted text" : "Hide quoted text";
      };
      applyCollapsed();
      toggle.addEventListener("click", () => {
        collapsed = !collapsed;
        applyCollapsed();
      });

      dom.append(attribution, toggle, host);

      return {
        dom,
        // Every event inside this node view is handled by the toggle
        // button itself; without this, ProseMirror's own mousedown
        // handling intercepts the click before it reaches the button.
        stopEvent: () => true,
        ignoreMutation: () => true,
      };
    };
  },
});
