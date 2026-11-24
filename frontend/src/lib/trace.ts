import type { Snapshot } from "@/api/client";

/**
 * Traceability over a snapshot's links: which model elements a requirement maps to, which
 * validation checks cover a record, and readable labels for any record id.
 */
export type Trace = {
  label: (id: string) => string;
  /** Model elements (tables, attributes, relationships) a requirement maps to. */
  mappedTo: (requirementId: string) => string[];
  /** Requirements mapped to any of these elements. */
  requirementsFor: (ids: string[]) => string[];
  /** Validation checks linked to any of these records. */
  checksFor: (ids: string[]) => Snapshot["checks"];
  /** What a check covers. */
  checked: (checkId: string) => string[];
  kindOf: (id: string) => string | undefined;
  /** Records this one links to with the given link type. */
  linksFrom: (id: string, linkType: string) => string[];
  /** Records that link to this one with the given link type. */
  linksTo: (id: string, linkType: string) => string[];
};

export function buildTrace(snap: Snapshot): Trace {
  const tables = new Map(snap.tables.map((t) => [t.id, t]));
  const attrs = new Map(snap.attributes.map((a) => [a.id, a]));
  const rels = new Map(snap.relationships.map((r) => [r.id, r]));
  const registers = new Map<string, { code: string | null; type: string }>();
  for (const [type, rows] of [["requirement", snap.requirements], ["definition", snap.definitions],
    ["question", snap.questions], ["decision", snap.decisions], ["check", snap.checks]] as const) {
    for (const r of rows) registers.set(r.id, { code: r.code, type });
  }
  const label = (id: string): string => {
    const t = tables.get(id);
    if (t) return t.data.name;
    const a = attrs.get(id);
    if (a) return `${tables.get(a.data.table_id)?.data.name ?? "?"}.${a.data.name}`;
    const r = rels.get(id);
    if (r) {
      const from = tables.get(r.data.from_table_id)?.data.name ?? "?";
      const to = tables.get(r.data.to_table_id)?.data.name ?? "?";
      return `${from} → ${to}${r.data.role_name ? ` (${r.data.role_name})` : ""}`;
    }
    return registers.get(id)?.code ?? id.slice(0, 8);
  };
  const kindOf = (id: string) =>
    tables.has(id) ? "table" : attrs.has(id) ? "attribute" : rels.has(id) ? "relationship" : registers.get(id)?.type;
  const links = snap.links;
  const checksById = new Map(snap.checks.map((c) => [c.id, c]));
  return {
    label,
    kindOf,
    mappedTo: (rid) => links.filter((l) => l.link_type === "maps_to" && l.from_id === rid).map((l) => l.to_id),
    requirementsFor: (ids) => {
      const set = new Set(ids);
      return [...new Set(links.filter((l) => l.link_type === "maps_to" && set.has(l.to_id)).map((l) => l.from_id))];
    },
    checksFor: (ids) => {
      const set = new Set(ids);
      const found = new Set(links.filter((l) => l.link_type === "checks" && set.has(l.to_id)).map((l) => l.from_id));
      return [...found].map((id) => checksById.get(id)).filter((c) => c !== undefined);
    },
    checked: (cid) => links.filter((l) => l.link_type === "checks" && l.from_id === cid).map((l) => l.to_id),
    linksFrom: (id, type) => links.filter((l) => l.link_type === type && l.from_id === id).map((l) => l.to_id),
    linksTo: (id, type) => links.filter((l) => l.link_type === type && l.to_id === id).map((l) => l.from_id),
  };
}
