/**
 * A deliberately small Markdown renderer.
 *
 * Brief prose is model-written text, so it is treated as untrusted: everything
 * is HTML-escaped first, raw HTML is never passed through, and the only link
 * that survives is an internal claim-provenance link of the exact shape the
 * citation callback produces. There is no `dangerouslySetInnerHTML` anywhere in
 * this app; this module returns data, and React renders it as text.
 */

export interface CitationToken {
  kind: "citation";
  claimId: string;
  version: number;
  label: string;
}

export interface TextToken {
  kind: "text";
  text: string;
  strong: boolean;
  code: boolean;
}

export type InlineToken = CitationToken | TextToken;

export interface HeadingBlock {
  kind: "heading";
  level: 1 | 2 | 3;
  tokens: InlineToken[];
}

export interface ParagraphBlock {
  kind: "paragraph";
  tokens: InlineToken[];
}

export interface ListBlock {
  kind: "list";
  items: InlineToken[][];
}

export type MarkdownBlock = HeadingBlock | ParagraphBlock | ListBlock;

const CLAIM_LINK =
  /\[([^\]\n]{1,120})\]\(\/claims\/(clm_[0-7][0-9A-HJKMNP-TV-Z]{25})\?version=(\d{1,6})\)/g;
const STRONG = /\*\*([^*\n]{1,300})\*\*/g;
const CODE = /`([^`\n]{1,300})`/g;

/** Strip anything that could be interpreted as markup by any consumer. */
export function sanitizeText(value: string): string {
  return value.replace(/[<>]/g, "");
}

function inline(text: string): InlineToken[] {
  const tokens: InlineToken[] = [];
  let cursor = 0;
  CLAIM_LINK.lastIndex = 0;
  for (const match of text.matchAll(CLAIM_LINK)) {
    const start = match.index ?? 0;
    if (start > cursor) tokens.push(...plain(text.slice(cursor, start)));
    tokens.push({
      kind: "citation",
      claimId: match[2],
      version: Number(match[3]),
      label: sanitizeText(match[1]),
    });
    cursor = start + match[0].length;
  }
  if (cursor < text.length) tokens.push(...plain(text.slice(cursor)));
  return tokens;
}

function plain(text: string): TextToken[] {
  // Any residual Markdown link is flattened to its label: an external URL is
  // never rendered as a link, because a Tycho brief cites claim versions.
  const flattened = text.replace(/\[([^\]\n]{0,120})\]\(([^)\n]{0,300})\)/g, "$1");
  const tokens: TextToken[] = [];
  let rest = sanitizeText(flattened);
  const emit = (pattern: RegExp, flag: "strong" | "code") => {
    const next: TextToken[] = [];
    let cursor = 0;
    pattern.lastIndex = 0;
    for (const match of rest.matchAll(pattern)) {
      const start = match.index ?? 0;
      if (start > cursor) {
        next.push({ kind: "text", text: rest.slice(cursor, start), strong: false, code: false });
      }
      next.push({
        kind: "text",
        text: match[1],
        strong: flag === "strong",
        code: flag === "code",
      });
      cursor = start + match[0].length;
    }
    if (cursor === 0) return;
    if (cursor < rest.length) {
      next.push({ kind: "text", text: rest.slice(cursor), strong: false, code: false });
    }
    tokens.push(...next);
    rest = "";
  };
  emit(STRONG, "strong");
  if (rest) emit(CODE, "code");
  if (rest) tokens.push({ kind: "text", text: rest, strong: false, code: false });
  return tokens.filter((token) => token.text.length > 0);
}

/** Parse a bounded subset: headings, paragraphs, and simple bullet lists. */
export function parseMarkdown(source: string): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = [];
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  let paragraph: string[] = [];
  let items: InlineToken[][] = [];

  const flushParagraph = () => {
    if (paragraph.length === 0) return;
    blocks.push({ kind: "paragraph", tokens: inline(paragraph.join(" ").trim()) });
    paragraph = [];
  };
  const flushList = () => {
    if (items.length === 0) return;
    blocks.push({ kind: "list", items });
    items = [];
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    const bullet = /^[-*]\s+(.*)$/.exec(line);
    if (heading) {
      flushParagraph();
      flushList();
      blocks.push({
        kind: "heading",
        level: heading[1].length as 1 | 2 | 3,
        tokens: inline(heading[2]),
      });
      continue;
    }
    if (bullet) {
      flushParagraph();
      items.push(inline(bullet[1]));
      continue;
    }
    if (line.trim() === "") {
      flushParagraph();
      flushList();
      continue;
    }
    flushList();
    paragraph.push(line.trim());
  }
  flushParagraph();
  flushList();
  return blocks;
}
