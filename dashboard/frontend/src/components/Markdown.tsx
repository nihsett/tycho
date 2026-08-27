import type { InlineToken, MarkdownBlock } from "../lib/markdown";
import { parseMarkdown } from "../lib/markdown";

interface Props {
  source: string;
  onOpenClaim: (claimId: string, version: number) => void;
}

function Inline({
  tokens,
  onOpenClaim,
}: {
  tokens: InlineToken[];
  onOpenClaim: Props["onOpenClaim"];
}) {
  return (
    <>
      {tokens.map((token, index) => {
        if (token.kind === "citation") {
          return (
            <button
              key={`${token.claimId}-${index}`}
              type="button"
              className="claim-link"
              onClick={() => onOpenClaim(token.claimId, token.version)}
            >
              View evidence
            </button>
          );
        }
        if (token.strong) return <strong key={index}>{token.text}</strong>;
        if (token.code) return <code key={index}>{token.text}</code>;
        return <span key={index}>{token.text}</span>;
      })}
    </>
  );
}

function Block({ block, onOpenClaim }: { block: MarkdownBlock; onOpenClaim: Props["onOpenClaim"] }) {
  if (block.kind === "heading") {
    const content = <Inline tokens={block.tokens} onOpenClaim={onOpenClaim} />;
    if (block.level === 1) return <h1>{content}</h1>;
    if (block.level === 2) return <h2>{content}</h2>;
    return <h3>{content}</h3>;
  }
  if (block.kind === "list") {
    return (
      <ul>
        {block.items.map((tokens, index) => (
          <li key={index}>
            <Inline tokens={tokens} onOpenClaim={onOpenClaim} />
          </li>
        ))}
      </ul>
    );
  }
  return (
    <p>
      <Inline tokens={block.tokens} onOpenClaim={onOpenClaim} />
    </p>
  );
}

/**
 * Renders brief prose as React elements only. There is no
 * `dangerouslySetInnerHTML` here or anywhere else in the app, so a grounded
 * quote or model sentence can never execute markup.
 */
export function Markdown({ source, onOpenClaim }: Props) {
  const blocks = parseMarkdown(source);
  return (
    <div className="brief-body">
      {blocks.map((block, index) => (
        <Block key={index} block={block} onOpenClaim={onOpenClaim} />
      ))}
    </div>
  );
}
