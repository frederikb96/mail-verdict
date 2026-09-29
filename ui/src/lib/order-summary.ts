/**
 * A tiny markdown subset for an order's summary -- never
 * dangerouslySetInnerHTML, no library. Only what the write call's own
 * prompt asks the model to produce: paragraphs, a bullet list, and
 * **bold**. Everything else (#, [, <, a bare URL) is plain text; no link
 * is ever produced from this text.
 */

export interface Inline {
  text: string;
  bold: boolean;
}

export type Block =
  | { kind: "paragraph"; inlines: Inline[] }
  | { kind: "bullets"; items: Inline[][] };

const BOLD_RE = /\*\*(.+?)\*\*/g;

function parseInlines(line: string): Inline[] {
  const inlines: Inline[] = [];
  let lastIndex = 0;
  for (const match of line.matchAll(BOLD_RE)) {
    const index = match.index ?? 0;
    if (index > lastIndex) inlines.push({ text: line.slice(lastIndex, index), bold: false });
    inlines.push({ text: match[1], bold: true });
    lastIndex = index + match[0].length;
  }
  if (lastIndex < line.length) inlines.push({ text: line.slice(lastIndex), bold: false });
  return inlines.length > 0 ? inlines : [{ text: "", bold: false }];
}

function isBulletLine(line: string): boolean {
  return line.startsWith("- ") || line.startsWith("* ");
}

export function parseOrderSummary(text: string): Block[] {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let paragraphLines: string[] = [];
  let bulletItems: Inline[][] = [];

  const flushParagraph = () => {
    if (paragraphLines.length === 0) return;
    blocks.push({ kind: "paragraph", inlines: parseInlines(paragraphLines.join(" ")) });
    paragraphLines = [];
  };
  const flushBullets = () => {
    if (bulletItems.length === 0) return;
    blocks.push({ kind: "bullets", items: bulletItems });
    bulletItems = [];
  };

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (line === "") {
      flushParagraph();
      flushBullets();
      continue;
    }
    if (isBulletLine(line)) {
      flushParagraph();
      bulletItems.push(parseInlines(line.slice(2)));
      continue;
    }
    flushBullets();
    paragraphLines.push(line);
  }
  flushParagraph();
  flushBullets();
  return blocks;
}
