import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { Calendar, Database, GitMerge, Hash, Key, KeyRound, Link2, Table, TrendingUp } from "lucide-react";
import { memo } from "react";
import type { AttributeRecord, TableRecord } from "@/api/client";
import { cn } from "@/lib/utils";

/** Validation checks on the table or its attributes, by status (from the Validator's findings). */
export type TableValidation = { failed: number; unverified: number; blocking: number; summaries: string[] };

export type TableNodeData = {
  table: TableRecord;
  attributes: AttributeRecord[];
  highlighted?: boolean;
  connectable: boolean;
  validation?: TableValidation;
};

export type TableFlowNode = Node<TableNodeData, "table">;

const KIND_STYLE = {
  fact: { icon: Database, label: "Fact", header: "bg-purple-500/10 dark:bg-purple-900/30 border-purple-600 dark:border-purple-700", badge: "bg-purple-500/20 text-purple-700 dark:text-purple-300", iconColor: "text-purple-500" },
  dimension: { icon: Table, label: "Dimension", header: "bg-blue-500/10 dark:bg-blue-900/30 border-blue-600 dark:border-blue-700", badge: "bg-blue-500/20 text-blue-700 dark:text-blue-300", iconColor: "text-blue-500" },
  bridge: { icon: GitMerge, label: "Bridge", header: "bg-amber-500/10 dark:bg-amber-900/30 border-amber-600 dark:border-amber-700", badge: "bg-amber-500/20 text-amber-700 dark:text-amber-300", iconColor: "text-amber-500" },
} as const;

function RoleIcon({ role }: { role: AttributeRecord["data"]["role"] }) {
  switch (role) {
    case "surrogate_key":
      return <Key className="size-3.5 text-yellow-400" aria-label="Surrogate key" />;
    case "natural_key":
      return <KeyRound className="size-3.5 text-orange-400" aria-label="Natural key" />;
    case "foreign_key":
      return <Link2 className="size-3.5 text-blue-400" aria-label="Foreign key" />;
    case "measure":
      return <TrendingUp className="size-3.5 text-green-400" aria-label="Measure" />;
    case "degenerate_dimension":
      return <Hash className="size-3.5 text-slate-400" aria-label="Degenerate dimension" />;
    case "scd_effective_from":
    case "scd_effective_to":
    case "scd_current_flag":
      return <Calendar className="size-3.5 text-purple-400" aria-label="SCD tracking" />;
    default:
      return null;
  }
}

/** Red for a failed check, amber for an unverified one; the tooltip lists them. */
function ValidationDot({ validation }: { validation: TableValidation }) {
  const failed = validation.failed > 0;
  const parts = [failed && `${validation.failed} failed`, validation.unverified > 0 && `${validation.unverified} unverified`]
    .filter(Boolean).join(", ");
  const title = `Validation: ${parts}${validation.blocking ? ` (${validation.blocking} blocking)` : ""}\n${validation.summaries.join("\n")}`;
  return (
    <span title={title} role="img" aria-label={`Validation: ${parts}`}
      className={cn("size-2.5 rounded-full shrink-0 ring-2 ring-card", failed ? "bg-red-500" : "bg-amber-500")} />
  );
}

function TableNode({ data, selected }: NodeProps<TableFlowNode>) {
  const { table, attributes, highlighted, connectable, validation } = data;
  const style = KIND_STYLE[table.data.kind];
  const Icon = style.icon;
  const sorted = [...attributes].sort((a, b) => a.data.ordinal - b.data.ordinal || a.data.name.localeCompare(b.data.name));

  return (
    <div
      className={cn(
        "bg-card border-2 rounded-lg shadow-lg w-[320px] transition-all duration-150",
        selected ? "border-blue-500" : highlighted ? "border-cyan-400 shadow-[0_0_12px_2px_rgba(34,211,238,0.5)]" : "border-border",
      )}
    >
      <Handle type="target" position={Position.Top} isConnectable={connectable} className="!size-3" />
      <Handle type="source" position={Position.Bottom} isConnectable={connectable} className="!size-3" />

      <div className={cn("px-4 py-2.5 border-b-2 rounded-t-lg", style.header)}>
        <div className="flex items-center gap-2">
          <Icon className={cn("size-5 shrink-0", style.iconColor)} />
          <div className="font-semibold truncate flex-1">{table.data.name}</div>
          {validation && (validation.failed > 0 || validation.unverified > 0) && <ValidationDot validation={validation} />}
          <span className={cn("text-xs px-2 py-0.5 rounded-full font-medium", style.badge)}>{style.label}</span>
        </div>
        {table.data.grain && (
          <div className="text-xs text-muted-foreground mt-1 line-clamp-2" title={table.data.grain}>
            Grain: {table.data.grain}
          </div>
        )}
        {table.data.kind === "fact" && !table.data.grain && (
          <div className="text-xs text-amber-500 mt-1">No grain defined</div>
        )}
      </div>

      <div className="py-1">
        {sorted.length === 0 ? (
          <div className="px-4 py-4 text-center text-muted-foreground text-sm">No attributes yet</div>
        ) : (
          sorted.map((a) => (
            <div key={a.id} className="flex items-center gap-2 px-4 py-1.5 border-b border-border/50 last:border-b-0">
              <div className="w-4 shrink-0 flex justify-center">
                <RoleIcon role={a.data.role} />
              </div>
              <div className="flex-1 min-w-0 text-sm font-medium truncate">{a.data.name}</div>
              <div className="text-xs text-muted-foreground shrink-0">
                {a.data.logical_type}
                {!a.data.is_nullable && " · required"}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

export default memo(TableNode);
