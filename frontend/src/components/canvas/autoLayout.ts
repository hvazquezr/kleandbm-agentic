/**
 * Star radial layout, adapted from the legacy editor: fact tables stacked at the centre,
 * dimensions and bridges on a circle large enough that adjacent nodes don't overlap.
 * Returns top-left positions keyed by table record id.
 */

const NODE_WIDTH = 340;
const HEADER_HEIGHT = 72;
const ROW_HEIGHT = 40;

const nodeHeight = (attributeCount: number) => HEADER_HEIGHT + Math.max(1, attributeCount) * ROW_HEIGHT;

export type LayoutTable = { id: string; kind: string; attributeCount: number };
export type Positions = Record<string, { x: number; y: number }>;

export function computeAutoLayout(tables: LayoutTable[]): Positions {
  const result: Positions = {};
  const facts = tables.filter((t) => t.kind === "fact");
  const others = tables.filter((t) => t.kind !== "fact");

  const FACT_GAP = 60;
  const factTotal = facts.reduce((h, t) => h + nodeHeight(t.attributeCount) + FACT_GAP, 0) - (facts.length ? FACT_GAP : 0);
  let y = -factTotal / 2;
  for (const t of facts) {
    result[t.id] = { x: -NODE_WIDTH / 2, y };
    y += nodeHeight(t.attributeCount) + FACT_GAP;
  }

  const n = others.length;
  if (n === 0) return result;

  const maxDiag = others.reduce((m, t) => Math.max(m, Math.hypot(NODE_WIDTH, nodeHeight(t.attributeCount))), 0);
  const PADDING = 80;
  const fromSpacing = n > 1 ? (maxDiag + PADDING) / ((2 * Math.PI) / n) : 0;
  const fromFact = Math.hypot(NODE_WIDTH, factTotal) / 2 + maxDiag / 2 + PADDING;
  const radius = Math.max(fromSpacing, fromFact, 500);

  others.forEach((t, i) => {
    const angle = -Math.PI / 2 + (2 * Math.PI * i) / n;
    result[t.id] = {
      x: Math.cos(angle) * radius - NODE_WIDTH / 2,
      y: Math.sin(angle) * radius - nodeHeight(t.attributeCount) / 2,
    };
  });
  return result;
}
