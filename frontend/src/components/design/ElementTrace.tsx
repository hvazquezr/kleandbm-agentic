import { CheckCircle2, CircleHelp, XCircle } from "lucide-react";
import type { Trace } from "@/lib/trace";
import type { Snapshot } from "@/api/client";
import { humanize } from "@/lib/labels";

/** Small clickable reference to a record. */
export function RefChip({ id, trace, onSelect }: { id: string; trace: Trace; onSelect?: (id: string) => void }) {
  return (
    <button type="button" onClick={(e) => { e.stopPropagation(); onSelect?.(id); }}
      className="font-mono text-[11px] rounded border px-1 hover:bg-accent text-foreground max-w-full truncate">
      {trace.label(id)}
    </button>
  );
}

export function CheckIcon({ status }: { status: string }) {
  if (status === "passed") return <CheckCircle2 className="size-3.5 text-green-400 shrink-0" />;
  if (status === "failed") return <XCircle className="size-3.5 text-red-400 shrink-0" />;
  return <CircleHelp className="size-3.5 text-amber-400 shrink-0" />;
}

/** One validation check as a compact line. */
export function CheckLine({ check, onSelect }: { check: Snapshot["checks"][number]; onSelect?: (id: string) => void }) {
  return (
    <button type="button" onClick={(e) => { e.stopPropagation(); onSelect?.(check.id); }}
      className="flex items-start gap-1.5 text-left text-xs hover:underline min-w-0">
      <CheckIcon status={check.data.status} />
      <span className="min-w-0">
        <span className="font-mono">{check.code}</span> {humanize(check.data.check_type)}
        {check.data.is_blocking && check.data.status !== "passed" ? " · blocking" : ""}
        <span className="block text-muted-foreground line-clamp-2">{check.data.summary}</span>
      </span>
    </button>
  );
}

/**
 * Traceability for a model element (a table with its attributes, or a relationship): the
 * requirements it serves and the validation checks that cover it.
 */
export function ElementTrace({ ids, trace, onSelect }: { ids: string[]; trace: Trace; onSelect: (id: string) => void }) {
  const requirements = trace.requirementsFor(ids);
  const checks = trace.checksFor(ids);
  return (
    <section className="space-y-3">
      <div className="space-y-1.5">
        <h3 className="text-sm font-semibold">Requirements it serves</h3>
        {requirements.length === 0 ? <p className="text-sm text-muted-foreground">Not mapped to a requirement.</p> : (
          <div className="flex flex-wrap gap-1">{requirements.map((r) => <RefChip key={r} id={r} trace={trace} onSelect={onSelect} />)}</div>
        )}
      </div>
      <div className="space-y-1.5">
        <h3 className="text-sm font-semibold">Validation</h3>
        {checks.length === 0 ? <p className="text-sm text-muted-foreground">No validation checks cover it.</p> : (
          <div className="space-y-1.5">{checks.map((c) => <CheckLine key={c.id} check={c} onSelect={onSelect} />)}</div>
        )}
      </div>
    </section>
  );
}
