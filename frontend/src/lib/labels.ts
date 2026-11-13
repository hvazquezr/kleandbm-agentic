import type { Schemas } from "@/api/client";

type Options<T extends string> = { value: T; label: string }[];

export const TABLE_KINDS: Options<Schemas["TableData"]["kind"]> = [
  { value: "fact", label: "Fact" },
  { value: "dimension", label: "Dimension" },
  { value: "bridge", label: "Bridge" },
];

export const SCD_TYPES: Options<NonNullable<Schemas["TableData"]["scd_type"]>> = [
  { value: "none", label: "No history" },
  { value: "type_1", label: "SCD Type 1 (overwrite)" },
  { value: "type_2", label: "SCD Type 2 (track history)" },
];

export const LOGICAL_TYPES: Options<Schemas["AttributeData"]["logical_type"]> = [
  { value: "integer", label: "integer" },
  { value: "decimal", label: "decimal" },
  { value: "text", label: "text" },
  { value: "boolean", label: "boolean" },
  { value: "date", label: "date" },
  { value: "timestamp", label: "timestamp" },
  { value: "time", label: "time" },
];

export const ATTRIBUTE_ROLES: Options<NonNullable<Schemas["AttributeData"]["role"]>> = [
  { value: "surrogate_key", label: "Surrogate key" },
  { value: "natural_key", label: "Natural key" },
  { value: "foreign_key", label: "Foreign key" },
  { value: "measure", label: "Measure" },
  { value: "degenerate_dimension", label: "Degenerate dimension" },
  { value: "descriptive", label: "Descriptive" },
  { value: "scd_effective_from", label: "SCD effective from" },
  { value: "scd_effective_to", label: "SCD effective to" },
  { value: "scd_current_flag", label: "SCD current flag" },
];

export const ADDITIVITY: Options<NonNullable<Schemas["AttributeData"]["additivity"]>> = [
  { value: "additive", label: "Additive" },
  { value: "semi_additive", label: "Semi-additive" },
  { value: "non_additive", label: "Non-additive" },
];

export const CARDINALITIES: Options<NonNullable<Schemas["RelationshipData"]["cardinality"]>> = [
  { value: "many_to_one", label: "Many to one" },
  { value: "one_to_one", label: "One to one" },
  { value: "many_to_many", label: "Many to many" },
];

export const STATE_LABELS: Record<string, string> = {
  draft: "Draft",
  ready_for_review: "Ready for review",
  accepted: "Accepted",
};

/** "fully_supported" -> "fully supported" */
export const humanize = (value: string | null | undefined) => (value ?? "").replaceAll("_", " ");
