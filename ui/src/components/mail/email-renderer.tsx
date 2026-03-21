"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import DOMPurify from "dompurify";
import { useTheme } from "@/components/theme-provider";

interface EmailRendererProps {
  /** Sanitized HTML content from backend. */
  html?: string | null;
  /** Plain text fallback. */
  plainText?: string | null;
  /** Whether remote images are allowed. */
  imagesAllowed?: boolean;
}

/** CSS injected into the Shadow DOM for email rendering. */
function getEmailStyles(theme: "light" | "dark"): string {
  const isDark = theme === "dark";
  return `
    :host {
      display: block;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 14px;
      line-height: 1.6;
      word-wrap: break-word;
      overflow-wrap: break-word;
      color: ${isDark ? "#e4e4e7" : "#18181b"};
      background: ${isDark ? "#09090b" : "#ffffff"};
    }
    img {
      max-width: 100%;
      height: auto;
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
  `;
}

/**
 * Strip remote images from HTML unless allowed.
 * Preserves inline data: URIs and cid: references.
 */
function stripRemoteImages(html: string): {
  html: string;
  hadRemoteImages: boolean;
} {
  let hadRemoteImages = false;
  const stripped = html.replace(
    /<img\b[^>]*\bsrc\s*=\s*["']?(https?:\/\/[^"'\s>]+)["']?[^>]*\/?>/gi,
    () => {
      hadRemoteImages = true;
      return "";
    },
  );
  return { html: stripped, hadRemoteImages };
}

/** Linkify URLs in plain text. */
function linkifyText(text: string): string {
  const urlPattern = /(https?:\/\/[^\s<]+)/g;
  return text.replace(
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
  imagesAllowed = false,
}: EmailRendererProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const shadowRootRef = useRef<ShadowRoot | null>(null);
  const { resolvedTheme } = useTheme();
  const [hasBlockedImages, setHasBlockedImages] = useState(false);

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
      let processedHtml = DOMPurify.sanitize(html, {
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

      if (!imagesAllowed) {
        const result = stripRemoteImages(processedHtml);
        processedHtml = result.html;
        setHasBlockedImages(result.hadRemoteImages);
      } else {
        setHasBlockedImages(false);
      }

      content = processedHtml;
    } else if (plainText) {
      // Render plain text with preserved whitespace and linkified URLs
      const escaped = plainText
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
      content = `<pre style="white-space: pre-wrap; font-family: inherit; margin: 0;">${linkifyText(escaped)}</pre>`;
    } else {
      content = '<p style="color: #71717a; font-style: italic;">No content available</p>';
    }

    const styles = getEmailStyles(resolvedTheme);
    shadowRootRef.current.innerHTML = `<style>${styles}</style>${content}`;
  }, [html, plainText, imagesAllowed, resolvedTheme]);

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

    return () => {
      root.removeEventListener("click", handleClick);
      root.removeEventListener("error", handleImageError, true);
    };
  }, [html, plainText]);

  return (
    <div className="flex flex-col">
      {hasBlockedImages && (
        <div className="flex items-center gap-2 bg-amber-500/10 px-4 py-2 text-sm text-amber-600 dark:text-amber-400">
          <span>Remote images have been blocked for privacy.</span>
        </div>
      )}
      <div
        ref={hostRef}
        className="min-h-0 flex-1 overflow-auto px-4 py-2"
      />
    </div>
  );
}
