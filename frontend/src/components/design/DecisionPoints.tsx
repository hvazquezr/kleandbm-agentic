import { ChevronDown, ChevronRight, GitBranch, Loader2, UserCheck, UserX } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import type { DecisionPoint } from "@/api/client";
import { useDecisionPoints, useResolveDecisionPoint, type ProjectPath } from "@/api/hooks";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { Trace } from "@/lib/trace";
import { humanize } from "@/lib/labels";
import { cn } from "@/lib/utils";

const OUTCOME: Record<string, { label: string; tone: string }> = {
  selected: { label: "design selected", tone: "text-green-400" },
  partial: { label: "best partial design only", tone: "text-amber-400" },
  no_candidate: { label: "no alternative qualified", tone: "text-amber-400" },
  failed: { label: "search failed", tone: "text-red-400" },
  searching: { label: "searching", tone: "text-muted-foreground" },
};

const NODE_TONE: Record<string, string> = {
  selected: "text-green-400",
  kept: "text-foreground",
  pruned: "text-muted-foreground",
  rejected: "text-red-400",
  duplicate: "text-muted-foreground",
};

/**
 * The Architect's decision points: the technical conflict, the alternatives the bounded search
 * considered (with scores and why each was kept, pruned or rejected) and the outcome. A search
 * that ended without a selected design waits for a person to accept or reject the design as it
 * stands; who decided, when and why is shown once they have.
 */
export function DecisionPoints({ path, candidateId, canEdit, trace, onSelectRecord }: {
  path: ProjectPath; candidateId: string; canEdit: boolean; trace: Trace; onSelectRecord: (id: string) => void;
}) {
  const points = useDecisionPoints(path, candidateId);
  if (!points.data || points.data.length === 0) return null;
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold flex items-center gap-2"><GitBranch className="size-4" /> Decision points</h3>
      <ul className="space-y-2">
        {points.data.map((dp) => (
          <PointItem key={dp.id} path={path} point={dp} canEdit={canEdit} trace={trace} onSelectRecord={onSelectRecord} />
        ))}
      </ul>
    </section>
  );
}

function PointItem({ path, point: dp, canEdit, trace, onSelectRecord }: {
  path: ProjectPath; point: DecisionPoint; canEdit: boolean; trace: Trace; onSelectRecord: (id: string) => void;
}) {
  const undecided = (dp.status === "partial" || dp.status === "no_candidate") && !dp.resolution;
  const [open, setOpen] = useState(undecided);
  const outcome = OUTCOME[dp.status] ?? { label: dp.status, tone: "" };
  const considered = dp.nodes.filter((n) => n.depth > 0);
  return (
    <li className={cn("rounded-md border text-sm", undecided && "border-amber-500/40")}>
      <button type="button" className="w-full text-left p-2 flex items-start gap-2" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown className="size-3.5 mt-1 shrink-0" /> : <ChevronRight className="size-3.5 mt-1 shrink-0" />}
        <span className="min-w-0 flex-1 space-y-0.5">
          <span className="flex flex-wrap items-center gap-1.5">
            <Badge variant="outline" className="font-normal text-[11px] h-5">{humanize(dp.trigger)}</Badge>
            <span className={cn("text-xs", outcome.tone)}>{outcome.label}</span>
            {undecided && <Badge variant="destructive" className="h-5 text-[11px]">needs your decision</Badge>}
            {dp.resolution && <Resolved resolution={dp.resolution} compact />}
          </span>
          <span className={cn("block text-sm", !open && "truncate")}>{dp.question}</span>
        </span>
      </button>
      {open && (
        <div className="px-2 pb-2 pl-7 space-y-2 text-xs">
          <div className="flex flex-wrap gap-1 items-center">
            {dp.decision_id && <RefButton id={dp.decision_id} trace={trace} onSelect={onSelectRecord} />}
            {dp.question_id && <RefButton id={dp.question_id} trace={trace} onSelect={onSelectRecord} />}
            <span className="text-muted-foreground">
              {considered.length} alternatives · {String(dp.usage?.model_calls ?? 0)} model calls ·{" "}
              {Math.round(Number(dp.usage?.seconds ?? 0))}s{dp.stop_reason ? ` · ${dp.stop_reason}` : ""}
            </span>
          </div>
          <ul className="space-y-1">
            {considered.map((n) => (
              <li key={n.id} className="flex gap-2" style={{ paddingLeft: `${(n.depth - 1) * 12}px` }}>
                <span className={cn("shrink-0 w-16", NODE_TONE[n.status])}>{n.status}</span>
                <span className="min-w-0">
                  <span className={cn(n.status === "selected" && "font-medium")}>{n.choice}</span>
                  {typeof n.evaluation?.total === "number" && (
                    <span className="text-muted-foreground font-mono"> · {Number(n.evaluation.total).toFixed(2)}</span>
                  )}
                  {n.reason && <span className="block text-muted-foreground">{n.reason}</span>}
                </span>
              </li>
            ))}
          </ul>
          {dp.resolution ? <Resolved resolution={dp.resolution} /> : undecided && (
            canEdit ? <DecideForm path={path} point={dp} /> : (
              <p className="text-amber-400">Waiting for a project editor to accept or reject this design.</p>
            )
          )}
        </div>
      )}
    </li>
  );
}

function DecideForm({ path, point }: { path: ProjectPath; point: DecisionPoint }) {
  const resolve = useResolveDecisionPoint(path);
  const [note, setNote] = useState("");
  const decide = async (outcome: "accepted" | "rejected") => {
    try {
      await resolve.mutateAsync({ id: point.id, outcome, note: note.trim() || undefined });
      toast.success(outcome === "accepted" ? "Accepted. Your decision is recorded." : "Rejected. Ask the Architect to redesign it.");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Couldn't record the decision");
    }
  };
  return (
    <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-2 space-y-2">
      <p>
        {point.status === "partial"
          ? "The search could only produce a partial design. "
          : "No alternative qualified, so the design was left as the Architect had it. "}
        Accept the design as it stands, or reject it so the Architect redesigns it. Your decision is recorded under your name.
      </p>
      <Textarea value={note} onChange={(e) => setNote(e.target.value)} maxLength={2000} rows={2}
        placeholder="Why (optional, saved with the decision)" className="text-xs min-h-0" />
      <div className="flex gap-2">
        <Button size="sm" disabled={resolve.isPending} onClick={() => decide("accepted")}>
          {resolve.isPending ? <Loader2 className="size-4 animate-spin" /> : <UserCheck className="size-4" />} Accept design
        </Button>
        <Button size="sm" variant="ghost" disabled={resolve.isPending} onClick={() => decide("rejected")}>
          <UserX className="size-4" /> Reject
        </Button>
      </div>
    </div>
  );
}

function Resolved({ resolution: r, compact }: { resolution: NonNullable<DecisionPoint["resolution"]>; compact?: boolean }) {
  const accepted = r.outcome === "accepted";
  const Icon = accepted ? UserCheck : UserX;
  const when = new Date(r.resolved_at).toLocaleString();
  if (compact) {
    return (
      <span className={cn("inline-flex items-center gap-1 text-xs", accepted ? "text-green-400" : "text-red-400")}>
        <Icon className="size-3" /> {accepted ? "accepted" : "rejected"} by {r.user_name}
      </span>
    );
  }
  return (
    <div className={cn("rounded-md border p-2 flex gap-2", accepted ? "border-green-500/30 bg-green-500/10" : "border-red-500/30 bg-red-500/10")}>
      <Icon className={cn("size-4 shrink-0", accepted ? "text-green-400" : "text-red-400")} />
      <div className="min-w-0">
        <p>{accepted ? "Accepted" : "Rejected"} by <span className="font-medium">{r.user_name}</span> · {when}
          {r.seq != null && <span className="text-muted-foreground font-mono"> · seq {r.seq}</span>}</p>
        {r.note && <p className="text-muted-foreground">“{r.note}”</p>}
        {!accepted && <p className="text-muted-foreground">The model stays a Draft until the Architect redesigns this.</p>}
      </div>
    </div>
  );
}

function RefButton({ id, trace, onSelect }: { id: string; trace: Trace; onSelect: (id: string) => void }) {
  return (
    <button type="button" onClick={() => onSelect(id)} className="font-mono rounded border px-1 hover:bg-accent">
      {trace.label(id)}
    </button>
  );
}
