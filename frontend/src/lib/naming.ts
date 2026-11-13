/** Lowercase snake_case tokens: "Order Date" -> ["order", "date"]. */
function tokens(text: string): string[] {
  return text.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
}

/**
 * Suggest a role-playing foreign key name from a role and the referenced key, merging any
 * overlap between the end of the role and the start of the key:
 *   ("Order Date", "date_key") -> "order_date_key"
 *   ("Ship", "date_key")       -> "ship_date_key"
 *   ("", "customer_key")       -> "customer_key"
 */
export function suggestForeignKeyName(role: string, baseKey: string): string {
  const r = tokens(role);
  const b = tokens(baseKey);
  if (r.length === 0) return b.join("_");
  // Longest k where the last k role tokens equal the first k key tokens.
  let overlap = 0;
  for (let k = Math.min(r.length, b.length); k > 0; k--) {
    if (r.slice(-k).join("_") === b.slice(0, k).join("_")) {
      overlap = k;
      break;
    }
  }
  return [...r, ...b.slice(overlap)].join("_");
}
