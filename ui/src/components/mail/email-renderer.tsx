"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import DOMPurify from "dompurify";
import { Moon, Sun } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useTheme } from "@/components/theme-provider";

interface EmailRendererProps {
  /** Sanitized HTML content from backend. */
  html?: string | null;
  /** Plain text fallback. */
  plainText?: string | null;
  /** Identifies the message for remembering its dark-mode choice; the
   * toggle is not offered without one, since there is nothing to key the
   * memory on. */
  messageId?: string;
  /** Highlights every occurrence of this text in the rendered message --
   * the reading pane's own in-message find, since the browser's own find
   * cannot reach content inside this component's shadow root. Absent or
   * blank clears any existing highlight. */
  searchQuery?: string;
  /** Which occurrence (0-indexed, in document order) carries the active
   * highlight and is scrolled into view; ignored without a searchQuery. */
  activeMatchIndex?: number;
  /** Called after (re)computing matches for searchQuery, with the total
   * found -- what a find bar's own "n of m" and step buttons need. */
  onMatchCountChange?: (count: number) => void;
}

/** Which canvas a message body renders on -- see pickCanvas below. */
type Canvas = "light" | "dark";

/** A stylesheet's own dark-mode media query, or a color-scheme declaration
 * naming dark support -- both live in a message's own (now sanitised
 * rather than discarded) `<style>` block, or occasionally in an inline
 * style on the document's root element. A plain substring search over the
 * already-sanitised HTML is enough: this is a display default, not a
 * security decision, and the manual toggle always has the final say
 * regardless of what it returns. */
const DARK_MODE_MEDIA_RE = /@media[^{]*prefers-color-scheme\s*:\s*dark/i;
const COLOR_SCHEME_DARK_RE = /color-scheme\s*:[^;"'}]*\bdark\b/i;

function declaresDarkModeSupport(html: string): boolean {
  return DARK_MODE_MEDIA_RE.test(html) || COLOR_SCHEME_DARK_RE.test(html);
}

/** The two ways a root-level inline colour declaration goes wrong on a
 * canvas the sender did not choose -- see isDarkSafeMessage below. */
interface RootColorDeclaration {
  color: boolean;
  background: boolean;
}

/** How many levels into the document a colour declaration still counts as
 * "the message's own colour scheme" rather than an isolated inline style
 * on one link or one span deep inside it -- a template sets its overall
 * background and text colour on the outer wrapper table or the body
 * itself, within a couple of levels of the root. */
const ROOT_COLOR_SCAN_DEPTH = 2;

function collectRootColorDeclarations(doc: Document): RootColorDeclaration[] {
  const declarations: RootColorDeclaration[] = [];
  const walk = (el: Element, depth: number) => {
    if (depth > ROOT_COLOR_SCAN_DEPTH) return;
    const style = el.getAttribute("style");
    if (style) {
      const hasColor = /(?:^|;)\s*color\s*:/i.test(style);
      const hasBackground = /(?:^|;)\s*background(?:-color)?\s*:/i.test(style);
      if (hasColor || hasBackground) declarations.push({ color: hasColor, background: hasBackground });
    }
    for (const child of Array.from(el.children)) walk(child, depth + 1);
  };
  if (doc.body) walk(doc.body, 0);
  return declarations;
}

/**
 * Whether a message's own root-level inline colours read safely on either
 * canvas, judged from the colours that survive rather than from any
 * declared intent.
 *
 * A shadow root isolates rules but not inherited properties, so `color`
 * and `background` reach the message independently of each other -- a
 * template that sets one without the other is safe only on the canvas it
 * was written against, because the missing half comes from :host and a
 * dark :host supplies dark text or a dark background exactly where the
 * message assumed white. A template that always sets both together is
 * safe on either: its own pair dominates wherever it applies, and :host
 * only shows through the parts it left unstyled. Nothing declared at all
 * is the majority case and is judged unsafe -- there is no positive
 * evidence either way, and the failure mode of guessing wrong is opposite
 * in severity (mildly disappointing on light, unreadable on dark).
 */
function isDarkSafeMessage(html: string): boolean {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const declarations = collectRootColorDeclarations(doc);
  if (declarations.length === 0) return false;
  return declarations.every((d) => d.color === d.background);
}

/**
 * The canvas a given HTML message body renders on by default, before any
 * per-message choice is applied.
 *
 * A message that declares its own dark-mode support opens dark: it has
 * told us it restyles itself for a dark canvas, and the same declaration
 * survives sanitisation now (see sanitizer.py) rather than being stripped
 * before this component ever sees it. Everything else -- most mail, which
 * carries only inline styling -- falls back to judging the colours that
 * survive: dark only when the message's own root-level styling reads
 * safely on either canvas, light otherwise. Failing towards light is
 * deliberate: light is the canvas mail is written for, so a wrong guess
 * there is mildly disappointing, while the reverse guess is unreadable.
 *
 * The reader can always override this default with the per-message toggle
 * below, and the plain-text wrapper this component generates itself
 * carries no colours of its own, so it follows the app's own theme
 * instead of going through any of this.
 */
function pickCanvas(html: string | null | undefined, theme: Canvas): Canvas {
  if (!html) return theme;
  if (declaresDarkModeSupport(html)) return "dark";
  return isDarkSafeMessage(html) ? "dark" : "light";
}

const DARK_MODE_STORAGE_KEY = "mail-verdict:message-dark-mode";

/** Per-message dark-mode choices, remembered across reopening the same
 * message -- a toggle that resets on every open is an irritation rather
 * than a feature. Keyed by message id rather than sender or globally:
 * whether dark rendering looks right is a property of one message's own
 * markup, not of who sent it or of every other open message. Stored in
 * localStorage alongside the rest of this app's client-only view state
 * (theme, query cache) rather than as a database-backed preference --
 * purely cosmetic, and not worth a table that grows with the mailbox. */
function readStoredCanvasChoice(messageId: string): Canvas | null {
  try {
    const raw = window.localStorage.getItem(DARK_MODE_STORAGE_KEY);
    if (!raw) return null;
    const stored = JSON.parse(raw) as Record<string, Canvas>;
    return stored[messageId] ?? null;
  } catch {
    return null;
  }
}

function writeStoredCanvasChoice(messageId: string, canvas: Canvas): void {
  try {
    const raw = window.localStorage.getItem(DARK_MODE_STORAGE_KEY);
    const stored = raw ? (JSON.parse(raw) as Record<string, Canvas>) : {};
    stored[messageId] = canvas;
    window.localStorage.setItem(DARK_MODE_STORAGE_KEY, JSON.stringify(stored));
  } catch {
    // Private browsing or a full quota loses the memory, not the toggle.
  }
}

/** CSS injected into the Shadow DOM for email rendering.
 *
 * Exported for the compose editor's quoted-message node, which renders a
 * quoted or forwarded message's own HTML the same way -- isolated in a
 * shadow root, on the light canvas mail is always written against -- and
 * has no reason to keep a second copy of this ruleset. */
export function getEmailStyles(canvas: Canvas): string {
  const isDark = canvas === "dark";
  return `
    :host {
      display: block;
      /* A shadow root isolates styles but does not create a containing
         block on its own, so position: fixed in a message would resolve
         against the viewport rather than this box -- covering the whole
         application regardless of what the server-side sanitizer catches.
         Layout containment makes the host itself that containing block,
         confining any such declaration to the message pane it belongs to. */
      contain: layout paint;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 14px;
      line-height: 1.6;
      word-wrap: break-word;
      overflow-wrap: break-word;
      color: ${isDark ? "#e4e4e7" : "#18181b"};
      background: ${isDark ? "#09090b" : "#ffffff"};
    }
    img {
      /* max-width alone contains a wide image, but not a tall one: a
         square or portrait asset with no sizing of its own (common for a
         retina-exported logo, sized for print rather than a reading pane)
         still scales to the full container width, and at that width its
         height can dominate the whole pane before anything below it is
         visible. max-height on the same replaced element bounds the other
         axis the same way, and the browser applies both simultaneously
         while width/height stay auto -- no distortion, no cropping, just
         a hard ceiling on either dimension a sender left unconstrained. */
      max-width: 100%;
      max-height: 70vh;
      height: auto;
      object-fit: contain;
    }
    a {
      color: ${isDark ? "#60a5fa" : "#2563eb"};
    }
    table {
      max-width: 100%;
      border-collapse: collapse;
    }
    td, th {
      padding: 4px 8px;
    }
    blockquote {
      border-left: 3px solid ${isDark ? "#3f3f46" : "#d4d4d8"};
      margin: 0.5em 0;
      padding: 0.25em 1em;
      color: ${isDark ? "#a1a1aa" : "#71717a"};
    }
    pre {
      background: ${isDark ? "#18181b" : "#f4f4f5"};
      padding: 8px 12px;
      border-radius: 4px;
      overflow-x: auto;
      font-size: 13px;
    }
    hr {
      border: none;
      border-top: 1px solid ${isDark ? "#27272a" : "#e4e4e7"};
      margin: 1em 0;
    }
    /* collapseQuotedReply below injects this pair around an incoming
       reply's own quoted original -- the composer already collapses its
       *outgoing* quote the same way, in quoted-message-node.ts. */
    .quoted-reply-toggle {
      display: inline-block;
      font-size: 0.75rem;
      color: ${isDark ? "#60a5fa" : "#2563eb"};
      text-decoration: underline;
      cursor: pointer;
      background: none;
      border: none;
      padding: 0;
      margin: 0.25em 0;
    }
    /* highlightSearchMatches below wraps every occurrence of the reading
       pane's own in-message find in one of these; the active one gets a
       stronger, more saturated fill so stepping between matches is
       visible even though every match already stands out from the body. */
    mark.search-match {
      background: ${isDark ? "#78350f" : "#fef08a"};
      color: inherit;
    }
    mark.search-match-active {
      background: ${isDark ? "#f97316" : "#f97316"};
      color: #1c1917;
      box-shadow: 0 0 0 2px ${isDark ? "#fdba74" : "#c2410c"};
    }
  `;
}

/** Class names real mail clients already give a reply's own quoted
 * original -- Gmail's, mirrored by several others including this
 * application's own composer (see quoted-message-node.ts), Thunderbird's,
 * Yahoo Mail's and ProtonMail's. Matched on the element itself or a
 * couple of ancestors, since a wrapper sometimes carries the class while
 * the blockquote inside it does not. Deliberately not a generic "quote"
 * match: an editorial pull-quote or callout a sender wrote on purpose
 * carries no such class, and collapsing it would be wrong. */
const REPLY_QUOTE_CLASS_RE = /\b(?:gmail_quote|moz-cite-prefix|yahoo_quoted|protonmail_quote)\b/i;
const REPLY_QUOTE_ANCESTOR_DEPTH = 2;

/** Whether a <blockquote> is a reply's quoted original rather than an
 * ordinary quotation the sender wrote on purpose.
 *
 * `type="cite"` is the one signal nearly every mail client agrees on --
 * Apple Mail, Thunderbird and this application's own composer all mark a
 * reply's quote this way, and nothing else legitimately would (the
 * sanitizer allowlists `type` on a blockquote for exactly this reading).
 * The class-name check below catches the messages that omit it.
 */
function isReplyQuoteBlockquote(blockquote: Element): boolean {
  if (blockquote.getAttribute("type")?.toLowerCase() === "cite") return true;
  let node: Element | null = blockquote;
  for (let depth = 0; node && depth <= REPLY_QUOTE_ANCESTOR_DEPTH; depth += 1) {
    if (REPLY_QUOTE_CLASS_RE.test(node.className)) return true;
    node = node.parentElement;
  }
  return false;
}

/**
 * Collapse an incoming reply's own quoted original behind a toggle, the
 * same treatment the composer already gives an *outgoing* quote.
 *
 * Only the first blockquote in the message recognised by
 * isReplyQuoteBlockquote is touched -- a deeper, nested quote inside it
 * (a reply to a reply) is already inside what gets hidden, and a
 * qualifying blockquote that never appears leaves the message untouched.
 * The toggle button and the hidden wrapper are plain markup with no
 * behaviour of their own; wireQuotedReplyToggles below is what a click on
 * the button actually does, attached once this string is in the live DOM.
 */
function collapseQuotedReply(html: string): string {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const blockquote = Array.from(doc.querySelectorAll("blockquote")).find(isReplyQuoteBlockquote);
  if (!blockquote) return html;

  const toggle = doc.createElement("button");
  toggle.type = "button";
  toggle.className = "quoted-reply-toggle";
  toggle.textContent = "Show quoted text";

  const body = doc.createElement("div");
  body.className = "quoted-reply-body";
  body.hidden = true;

  blockquote.replaceWith(body);
  body.append(blockquote);
  body.before(toggle);

  return doc.body.innerHTML;
}

/** Attached once collapseQuotedReply's markup is in the live shadow DOM --
 * flips the adjacent quote's visibility and relabels the button that was
 * clicked, mirroring quoted-message-node.ts's own applyCollapsed. */
function wireQuotedReplyToggles(root: ShadowRoot): void {
  for (const toggle of root.querySelectorAll<HTMLButtonElement>(".quoted-reply-toggle")) {
    const body = toggle.nextElementSibling;
    if (!(body instanceof HTMLElement) || !body.classList.contains("quoted-reply-body")) continue;
    toggle.onclick = () => {
      body.hidden = !body.hidden;
      toggle.textContent = body.hidden ? "Show quoted text" : "Hide quoted text";
    };
  }
}

const SEARCH_MATCH_CLASS = "search-match";
const SEARCH_MATCH_ACTIVE_CLASS = "search-match-active";

/** Undo highlightSearchMatches below, restoring plain text nodes.
 *
 * Run before every new search rather than only when a query is cleared --
 * otherwise a second search over content the first one already marked up
 * would nest a <mark> inside a <mark>, doubling the count on each retype.
 */
function clearSearchHighlights(root: ShadowRoot): void {
  for (const mark of Array.from(root.querySelectorAll(`mark.${SEARCH_MATCH_CLASS}`))) {
    const parent = mark.parentNode;
    if (!parent) continue;
    parent.replaceChild(document.createTextNode(mark.textContent ?? ""), mark);
    parent.normalize();
  }
}

/**
 * Wrap every occurrence of `query` in the rendered message in a `<mark>`,
 * case-insensitively, and return them in document order.
 *
 * A shadow root is opaque to the browser's own in-page find -- it never
 * looks inside one -- which is the entire reason this exists rather than
 * leaving ctrl+F to the browser. Walking text nodes directly (rather than
 * matching against innerHTML, which would count and cut across tag
 * boundaries) is what keeps a match from ever splitting inside a tag.
 */
function highlightSearchMatches(root: ShadowRoot, query: string): HTMLElement[] {
  clearSearchHighlights(root);
  const trimmed = query.trim();
  if (!trimmed) return [];
  const lowerQuery = trimmed.toLowerCase();

  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => {
      const parentTag = node.parentElement?.tagName;
      return parentTag === "STYLE" || parentTag === "SCRIPT"
        ? NodeFilter.FILTER_REJECT
        : NodeFilter.FILTER_ACCEPT;
    },
  });
  const textNodes: Text[] = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    textNodes.push(node as Text);
  }

  const marks: HTMLElement[] = [];
  for (const textNode of textNodes) {
    const text = textNode.textContent ?? "";
    const lowerText = text.toLowerCase();
    if (!lowerText.includes(lowerQuery)) continue;

    const fragment = document.createDocumentFragment();
    let start = 0;
    let index = lowerText.indexOf(lowerQuery);
    while (index !== -1) {
      if (index > start) fragment.append(document.createTextNode(text.slice(start, index)));
      const mark = document.createElement("mark");
      mark.className = SEARCH_MATCH_CLASS;
      mark.textContent = text.slice(index, index + trimmed.length);
      fragment.append(mark);
      marks.push(mark);
      start = index + trimmed.length;
      index = lowerText.indexOf(lowerQuery, start);
    }
    if (start < text.length) fragment.append(document.createTextNode(text.slice(start)));
    textNode.parentNode?.replaceChild(fragment, textNode);
  }
  return marks;
}

/** Escape every character that can change the meaning of markup.
 *
 * Quotes matter as much as angle brackets here: the linkifier below places a
 * matched URL inside an href attribute, so an unescaped quote closes that
 * attribute and everything after it is parsed as further attributes -- which
 * is an event handler if the sender wants one.
 */
export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Linkify URLs in already-escaped plain text. */
export function linkifyText(escaped: string): string {
  // Stops at a quote entity as well as whitespace, so a URL cannot swallow
  // the boundary of the attribute it is about to be placed in.
  const urlPattern = /(https?:\/\/(?:(?!&quot;|&#39;|&lt;|&gt;)[^\s<>"'])+)/g;
  return escaped.replace(
    urlPattern,
    '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>',
  );
}

/**
 * Renders email HTML content in an isolated Shadow DOM.
 *
 * Uses Shadow DOM for complete CSS isolation (same approach as mail0).
 * Falls back to linkified plain text when no HTML is available.
 */
export function EmailRenderer({
  html,
  plainText,
  messageId,
  searchQuery,
  activeMatchIndex,
  onMatchCountChange,
}: EmailRendererProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const shadowRootRef = useRef<ShadowRoot | null>(null);
  const searchMatchesRef = useRef<HTMLElement[]>([]);
  const { resolvedTheme } = useTheme();
  // The reader's explicit choice for this message, overriding both the
  // light default and a sender's own dark declaration. Starts unset -- it
  // is loaded from localStorage in an effect below, once a messageId is
  // known and the client has mounted, never read during the initial
  // render (localStorage does not exist during a static export's build).
  const [manualCanvas, setManualCanvas] = useState<Canvas | null>(null);

  useEffect(() => {
    if (!messageId) {
      setManualCanvas(null);
      return;
    }
    setManualCanvas(readStoredCanvasChoice(messageId));
  }, [messageId]);

  // pickCanvas parses the message's own markup with DOMParser, which does
  // not exist during a static export's build -- computed in an effect
  // rather than during render, the same reason manualCanvas above is.
  // "light" is a safe, non-crashing placeholder for the render or two
  // before this settles, and also the correct fail-towards-light default
  // if a message ever has no HTML to judge.
  const [autoCanvas, setAutoCanvas] = useState<Canvas>("light");
  useEffect(() => {
    setAutoCanvas(pickCanvas(html, resolvedTheme));
  }, [html, resolvedTheme]);

  const canvas = manualCanvas ?? autoCanvas;

  const toggleCanvas = useCallback(() => {
    if (!messageId) return;
    const next: Canvas = canvas === "dark" ? "light" : "dark";
    setManualCanvas(next);
    writeStoredCanvasChoice(messageId, next);
  }, [canvas, messageId]);

  // Attach Shadow DOM once
  useEffect(() => {
    if (!hostRef.current || shadowRootRef.current) return;
    shadowRootRef.current = hostRef.current.attachShadow({ mode: "open" });
  }, []);

  // Render content into Shadow DOM
  useEffect(() => {
    if (!shadowRootRef.current) return;

    let content: string;

    if (html) {
      // Client-side sanitization as defense-in-depth (backend uses nh3)
      const processedHtml = DOMPurify.sanitize(html, {
        ALLOW_UNKNOWN_PROTOCOLS: false,
        ALLOWED_TAGS: [
          "a", "abbr", "address", "article", "b", "blockquote", "br",
          "caption", "center", "cite", "code", "col", "colgroup", "dd",
          "del", "details", "dfn", "div", "dl", "dt", "em", "figcaption",
          "figure", "font", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
          "header", "hr", "i", "img", "ins", "kbd", "li", "main", "mark",
          "nav", "ol", "p", "pre", "q", "s", "section", "small", "span",
          "strong", "sub", "summary", "sup", "table", "tbody", "td",
          "tfoot", "th", "thead", "tr", "u", "ul", "wbr",
        ],
        ALLOWED_ATTR: [
          "align", "alt", "border", "cellpadding", "cellspacing", "class",
          "color", "colspan", "dir", "face", "height", "href", "hspace",
          "id", "lang", "role", "rowspan", "size", "src", "style",
          "summary", "target", "title", "type", "valign", "vspace", "width",
        ],
      });

      content = collapseQuotedReply(processedHtml);
    } else if (plainText) {
      // Render plain text with preserved whitespace and linkified URLs
      // Sanitized like the HTML path rather than trusted for being "just
      // text": everything here is still assigned through innerHTML, so an
      // escaping mistake in the linkifier would be an injection rather than
      // a rendering glitch.
      const linked = linkifyText(escapeHtml(plainText));
      content = DOMPurify.sanitize(
        `<pre style="white-space: pre-wrap; font-family: inherit; margin: 0;">${linked}</pre>`,
        {
          ALLOW_UNKNOWN_PROTOCOLS: false,
          ALLOWED_TAGS: ["pre", "a", "br"],
          ALLOWED_ATTR: ["href", "target", "rel", "style"],
        },
      );
    } else {
      content = '<p style="color: #71717a; font-style: italic;">No content available</p>';
    }

    const styles = getEmailStyles(canvas);
    shadowRootRef.current.innerHTML = `<style>${styles}</style>${content}`;
  }, [html, plainText, canvas]);

  // Handle link clicks to open in new tab
  useEffect(() => {
    if (!shadowRootRef.current) return;
    const root = shadowRootRef.current;

    const handleClick = (e: Event) => {
      const target = e.target as HTMLElement;
      const anchor = target.closest("a");
      if (anchor) {
        e.preventDefault();
        const href = anchor.getAttribute("href");
        if (
          href &&
          (href.startsWith("http://") || href.startsWith("https://"))
        ) {
          window.open(href, "_blank", "noopener,noreferrer");
        } else if (href && href.startsWith("mailto:")) {
          window.location.href = href;
        }
      }
    };

    const handleImageError = (e: Event) => {
      const target = e.target as HTMLImageElement;
      if (target.tagName === "IMG") {
        target.style.display = "none";
      }
    };

    root.addEventListener("click", handleClick);
    root.addEventListener("error", handleImageError, true);
    wireQuotedReplyToggles(root);

    return () => {
      root.removeEventListener("click", handleClick);
      root.removeEventListener("error", handleImageError, true);
    };
  }, [html, plainText]);

  // Recompute the find highlight. Depends on html/plainText/canvas as well
  // as searchQuery because the content-render effect above rebuilds
  // shadowRoot.innerHTML on any of those and would otherwise wipe out
  // marks this effect isn't re-running to replace.
  useEffect(() => {
    if (!shadowRootRef.current) return;
    const root = shadowRootRef.current;
    const marks = searchQuery ? highlightSearchMatches(root, searchQuery) : [];
    if (!searchQuery) clearSearchHighlights(root);
    searchMatchesRef.current = marks;
    onMatchCountChange?.(marks.length);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchQuery, html, plainText, canvas]);

  // Move the active highlight without recomputing every match -- stepping
  // between them is one class toggle plus a scroll, not a re-search. Also
  // depends on whatever the highlighting effect above depends on: within
  // one commit React runs effects in declaration order, so by the time
  // this one reads searchMatchesRef the effect above has already
  // refreshed it for the new query -- without that, a fresh search leaves
  // its first match with no active highlight until activeMatchIndex next
  // changes on its own.
  useEffect(() => {
    const marks = searchMatchesRef.current;
    marks.forEach((mark, index) => {
      mark.classList.toggle(SEARCH_MATCH_ACTIVE_CLASS, index === activeMatchIndex);
    });
    if (activeMatchIndex != null) {
      marks[activeMatchIndex]?.scrollIntoView({ block: "center", behavior: "smooth" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeMatchIndex, searchQuery, html, plainText, canvas]);

  return (
    <div className="flex flex-col">
      {/* The blocked-images notice a reader actually sees is ImageBanner,
          driven by the server's own has_blocked_images -- this component
          never decides that itself. */}
      {/* html messages render on their own fixed canvas rather than the
          app's theme (see pickCanvas), so the choice needs a control of
          its own -- plain text already follows the theme and gets none. */}
      {html && messageId && (
        <div className="flex justify-end px-4 pt-2">
          <Button
            variant="ghost"
            size="sm"
            className="h-6 gap-1.5 px-2 text-xs text-muted-foreground"
            onClick={toggleCanvas}
            title={
              canvas === "dark"
                ? "Switch this message to light mode"
                : "Enable dark message mode"
            }
            aria-label={
              canvas === "dark"
                ? "Switch this message to light mode"
                : "Enable dark message mode"
            }
          >
            {canvas === "dark" ? (
              <Sun className="h-3 w-3" />
            ) : (
              <Moon className="h-3 w-3" />
            )}
            {canvas === "dark" ? "Light mode" : "Dark mode"}
          </Button>
        </div>
      )}
      <div
        ref={hostRef}
        data-testid="email-body"
        className="min-h-0 flex-1 overflow-auto px-4 py-2"
      />
    </div>
  );
}
