interface Props {
  scopes: string[];
}

interface OntologyNode {
  key: string;
  segment: string;
  children: OntologyNode[];
}

const LABELS: Record<string, string> = {
  gtm: "Go to market",
};

function branchLabel(segment: string): string {
  return LABELS[segment] ?? `${segment.charAt(0).toUpperCase()}${segment.slice(1)}`;
}

export function buildOntology(scopes: string[]): OntologyNode[] {
  const roots: OntologyNode[] = [];

  for (const scope of scopes) {
    let siblings = roots;
    const path: string[] = [];

    for (const segment of scope.split("/").filter(Boolean)) {
      path.push(segment);
      const key = path.join("/");
      let node = siblings.find((candidate) => candidate.segment === segment);
      if (!node) {
        node = { key, segment, children: [] };
        siblings.push(node);
      }
      siblings = node.children;
    }
  }

  return roots;
}

function Branch({ node }: { node: OntologyNode }) {
  return (
    <li className="ontology-branch">
      <span>{branchLabel(node.segment)}</span>
      {node.children.length > 0 ? (
        <ul className="ontology-children">
          {node.children.map((child) => (
            <Branch key={child.key} node={child} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export function OntologyMap({ scopes }: Props) {
  const branches = buildOntology(scopes);

  return (
    <section className="ontology-map" aria-labelledby="ontology-map-heading">
      <div className="ontology-copy">
        <p className="eyebrow">Durable knowledge model</p>
        <h2 id="ontology-map-heading">Every entity has a versioned belief tree.</h2>
        <p>
          Evidence updates a claim version or supersedes it. The history is preserved,
          never rewritten.
        </p>
      </div>

      <div className="ontology-visual">
        <div className="ontology-tree">
          <div className="ontology-root">
            <strong>Entity</strong>
            <span>one model per competitor</span>
          </div>
          {branches.length > 0 ? (
            <ul className="ontology-branches" aria-label="Governed ontology branches">
              {branches.map((node) => (
                <Branch key={node.key} node={node} />
              ))}
            </ul>
          ) : (
            <p className="ontology-loading">Loading governed branches…</p>
          )}
        </div>

        <ol className="knowledge-chain" aria-label="Durable claim chain">
          <li>Claim ID</li>
          <li>Version</li>
          <li>Evidence</li>
          <li>Supersession history</li>
        </ol>
      </div>
    </section>
  );
}
