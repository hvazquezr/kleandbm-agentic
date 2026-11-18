import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, CircleHelp, Loader2, ScrollText, ShieldCheck, UserPen, XCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import type { Candidate, Snapshot } from "@/api/client";
import { useCandidate, useCandidateAction, useCandidates, useRunAssessments, type ProjectPath } from "@/api/hooks";
import { FlaggedBanner } from "@/components/runs/Assessment";
import { FlaggedContent } from "@/components/runs/FlaggedContent";
import { useConfirm } from "@/components/editor/useConfirm";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { humanize } from "@/lib/labels";
import { buildTrace } from "@/lib/trace";
import { DecisionPoints } from "./DecisionPoints";
import { cn } from "@/lib/utils";

/** A Validator finding, as stored on the candidate's review rounds. */
export type Finding = {
  issue_key: string;
  check_type: string;
  status: "passed" | "failed" | "unverified";
  is_blocking: boolean;
  summary: string;
  elements: string[];
  requirement_ids: string[];
  question_ids?: string[];
  evidence: { kind: "source" | "document" | "internal_check"; evidence_id?: string; tool?: string; citation?: string; name?: string }[];
  impact: string | null;
  recommended_action: string | null;
  edited_by_person?: boolean;
};
type Round = { round: number; run_id: string; model: string; status: string; detail: string | null; summary: string | null; findings: Finding[] };

const STATUS_TONE: Record<string, string> = {
  passed: "text-green-400",
  failed: "text-red-400",
  unverified: "text-amber-400",
};

const CANDIDATE_STATUS: Record<string, string> = {
  building: "The Architect is still working on this design.",
  proposed: "Proposed: review the changes and apply them.",
  applied: "Applied to the model.",
  discarded: "Discarded.",
  stale: "Out of date: the model changed after this design was made. Ask the Architect for a new one.",
  failed: "The run ended before the design was finished.",
};

/**
 * The Architect's latest design candidate and the Validator's review of it: what applying it
 * would change, whether it can become Ready for Review and what blocks it, each finding with
 * its evidence and recommended fix, and the Architect's own structural checks. A person
 * applies or discards a proposal; nothing here accepts the model.
 */
export function ReviewPanel({
  path,
  modelId,
  snapshot,
  canEdit,
  focusCandidateId,
  onSelectRecord,
  canViewRuns = false,
  onOpenRun,
}: {
  path: ProjectPath;
  modelId: string;
  snapshot: Snapshot;
  canEdit: boolean;
  /** Show this candidate (e.g. the one an Architect run just produced). */
  focusCandidateId?: string;
  onSelectRecord: (id: string) => void;
  /** Transcripts and monitor assessments need edit access. */
  canViewRuns?: boolean;
  /** Open a run's transcript, optionally at one of its child runs (a Validator round). */
  onOpenRun?: (runId: string, childRunId?: string) => void;
}) {
  const candidates = useCandidates(path, modelId);
  const [chosen, setChosen] = useState<string | undefined>(focusCandidateId);
  useEffect(() => { if (focusCandidateId) setChosen(focusCandidateId); }, [focusCandidateId]);
  const list = candidates.data ?? [];
  const current = chosen ?? list[0]?.id;
  const candidate = useCandidate(path, current);

  const flagged = <FlaggedContent path={path} canEdit={canEdit} />;
  if (candidates.isLoading) return <p className="text-sm text-muted-foreground">Loading…</p>;
  if (list.length === 0) {
    return (
      <div className="space-y-5">
      {flagged}
      <div className="text-sm text-muted-foreground space-y-2 rounded-md border border-dashed p-3">
        <p className="font-medium text-foreground flex items-center gap-2"><ShieldCheck className="size-4" /> No designs yet</p>
        <p>Ask the Architect for a draft on the Assistant tab. Its design and the Validator's review appear here.</p>
      </div>
      </div>
    );
  }
  return (
    <div className="space-y-5">
      {flagged}
      {list.length > 1 && (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <span>Design</span>
          <select className="h-7 rounded-md border bg-background px-2 text-xs text-foreground" value={current}
            onChange={(e) => setChosen(e.target.value)}>
            {list.map((c, i) => (
              <option key={c.id} value={c.id}>
                {i === 0 ? "Latest" : new Date(c.created_at).toLocaleString()} · {c.status} · {c.tables} tables
              </option>
            ))}
          </select>
        </div>
      )}
      {candidate.data ? (
        <CandidateView path={path} candidate={candidate.data} snapshot={snapshot} canEdit={canEdit} onSelectRecord={onSelectRecord}
          canViewRuns={canViewRuns} onOpenRun={onOpenRun} />
      ) : <p className="text-sm text-muted-foreground">Loading…</p>}
    </div>
  );
}

function CandidateView({ path, candidate, snapshot, canEdit, onSelectRecord, canViewRuns, onOpenRun }: {
  path: ProjectPath; candidate: Candidate; snapshot: Snapshot; canEdit: boolean; onSelectRecord: (id: string) => void;
  canViewRuns: boolean; onOpenRun?: (runId: string, childRunId?: string) => void;
}) {
  const assessments = useRunAssessments(path, candidate.run_id ?? undefined, canViewRuns);
  const openRun = candidate.run_id && canViewRuns && onOpenRun
    ? (child?: string) => onOpenRun(candidate.run_id!, child) : undefined;
  const apply = useCandidateAction(path, "apply");
  const discard = useCandidateAction(path, "discard");
  const [confirm, dialog] = useConfirm();
  const rounds = (candidate.validation ?? []) as Round[];
  const last = rounds[rounds.length - 1];
  const open = candidate.status === "proposed" || candidate.status === "building";
  const codes = useMemo(() => {
    const m = new Map<string, { code: string | null; id: string }>();
    for (const r of [...snapshot.requirements, ...snapshot.questions]) m.set(r.id, { code: r.code, id: r.id });
    return m;
  }, [snapshot]);
  const trace = useMemo(() => buildTrace(snapshot), [snapshot]);

  const doApply = async () => {
    try {
      await apply.mutateAsync(candidate.id);
      toast.success(candidate.ready_for_review ? "Applied. The model is Ready for Review." : "Applied. The model stays a Draft.");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Couldn't apply the design");
    }
  };
  const doDiscard = async () => {
    if ((await confirm({ title: "Discard this design?", description: "The live model isn't changed. You can ask the Architect for a new design.", confirmLabel: "Discard", destructive: true })) === null) return;
    try {
      await discard.mutateAsync(candidate.id);
      toast.success("Design discarded");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Couldn't discard the design");
    }
  };

  return (
    <div className="space-y-5">
      {dialog}
      {canViewRuns && <FlaggedBanner assessment={assessments.data?.[0]} onOpen={openRun && (() => openRun())} />}
      <section className="space-y-2">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold">Design</h3>
          {openRun && (
            <button type="button" className="ml-auto inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
              onClick={() => openRun()}>
              <ScrollText className="size-3" /> Architect run
            </button>
          )}
          <Badge variant="outline" className="font-normal">{candidate.status}</Badge>
          <span className="text-xs text-muted-foreground font-mono">from seq {candidate.base_seq}{candidate.applied_seq ? ` → seq ${candidate.applied_seq}` : ""}</span>
        </div>
        <p className={cn("text-sm", candidate.stale || candidate.status === "stale" ? "text-amber-400" : "text-muted-foreground")}>
          {candidate.stale ? CANDIDATE_STATUS.stale : CANDIDATE_STATUS[candidate.status] ?? candidate.status}
        </p>
        {candidate.review_outdated && !candidate.stale && (
          <p className="text-sm text-amber-400">
            {candidate.status === "applied" ? "" : "You can still apply this design, but it stays a Draft. "}
            Ask the Architect to revise it against the current registers.
          </p>
        )}
        <Readiness candidate={candidate} reviewed={!!last} state={snapshot.state} />
      </section>

      {open && candidate.diff && (
        <section className="space-y-2">
          <h3 className="text-sm font-semibold">What applying changes</h3>
          <DiffList label="Added" tone="text-green-400" items={candidate.diff.added} />
          <DiffList label="Changed" tone="text-amber-400" items={candidate.diff.changed} />
          <DiffList label="Removed" tone="text-red-400" items={candidate.diff.removed} />
          {canEdit && (
            <div className="flex gap-2 pt-1">
              <Button size="sm" disabled={apply.isPending || candidate.stale} onClick={doApply}>
                {apply.isPending && <Loader2 className="size-4 animate-spin" />} Apply as one change
              </Button>
              <Button size="sm" variant="ghost" disabled={discard.isPending} onClick={doDiscard}>Discard</Button>
            </div>
          )}
        </section>
      )}
      {open && candidate.stale && canEdit && (
        <Button size="sm" variant="ghost" disabled={discard.isPending} onClick={doDiscard}>Discard this design</Button>
      )}

      <DecisionPoints path={path} candidateId={candidate.id} canEdit={canEdit} trace={trace} onSelectRecord={onSelectRecord} />

      <section className="space-y-2">
        <h3 className="text-sm font-semibold flex items-center gap-2"><ShieldCheck className="size-4" /> Validator review</h3>
        {!last && <p className="text-sm text-muted-foreground">Not reviewed, so the design is unverified.</p>}
        {last && <RoundView round={last} codes={codes} onSelectRecord={onSelectRecord} onOpen={openRun && (() => openRun(last.run_id))} />}
        {rounds.length > 1 && <EarlierRounds rounds={rounds.slice(0, -1)} attempts={candidate.issue_attempts} />}
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Architect's internal checks</h3>
        <ul className="space-y-1">
          {(candidate.internal_checks as { name: string; status: string; hard: boolean; summary: string }[]).map((c) => (
            <li key={c.name} className="flex items-start gap-2 text-sm">
              <StatusIcon status={c.status} />
              <span className="min-w-0">
                <span className="font-mono text-xs">{c.name}</span>{c.hard && <span className="text-xs text-muted-foreground"> · hard</span>}
                <span className="block text-xs text-muted-foreground">{c.summary}</span>
              </span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

function Readiness({ candidate, reviewed, state }: { candidate: Candidate; reviewed: boolean; state: Snapshot["state"] }) {
  // Once a design is applied, the model's lifecycle moves on without it: a person accepts it,
  // or a later change (to the model or the shared registers) returns it to Draft. Say what's
  // true now, not what was true when the design was applied.
  if (candidate.status === "applied") {
    if (state.effective_state === "accepted") {
      return (
        <div className="rounded-md border border-blue-500/30 bg-blue-500/10 p-2.5 text-sm flex gap-2">
          <CheckCircle2 className="size-4 text-blue-300 shrink-0 mt-0.5" />
          <span>
            Accepted by {state.last_event?.by_name ?? "a project member"} at seq {state.recorded_at_seq}
            {state.last_event && <> on {new Date(state.last_event.at).toLocaleDateString()}</>}
            {state.last_event?.note ? <>: “{state.last_event.note}”</> : "."}
          </span>
        </div>
      );
    }
    if (state.effective_state === "draft" && candidate.ready_for_review) {
      return (
        <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-2.5 text-sm flex gap-2">
          <AlertTriangle className="size-4 text-amber-400 shrink-0 mt-0.5" />
          <span>
            Nothing blocked review when this design was applied (seq {candidate.applied_seq}), but the model or its
            registers changed since{state.changed_since_state && state.recorded_at_seq ? ` seq ${state.recorded_at_seq}` : ""}, so it's a Draft again.
            Mark it Ready for Review or accept it under Versions.
          </span>
        </div>
      );
    }
  }
  if (candidate.ready_for_review) {
    return (
      <div className="rounded-md border border-green-500/30 bg-green-500/10 p-2.5 text-sm flex gap-2">
        <CheckCircle2 className="size-4 text-green-400 shrink-0 mt-0.5" />
        <span>Nothing blocks review{candidate.status === "applied" ? ", so the model is Ready for Review" : ": applying it makes the model Ready for Review"}. Accepting it is a person's decision.</span>
      </div>
    );
  }
  return (
    <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-2.5 text-sm space-y-1">
      <div className="flex gap-2 font-medium"><AlertTriangle className="size-4 text-amber-400 shrink-0 mt-0.5" /> Stays a Draft until these are resolved</div>
      <ul className="list-disc pl-8 text-xs space-y-0.5">
        {candidate.blockers.map((b) => <li key={b}>{b}</li>)}
      </ul>
      {!reviewed && <p className="text-xs text-muted-foreground pl-6">Configure a Validator model to review designs.</p>}
    </div>
  );
}

function DiffList({ label, tone, items }: { label: string; tone: string; items: string[] }) {
  const [open, setOpen] = useState(items.length <= 8);
  if (items.length === 0) return null;
  return (
    <div className="text-sm">
      <button type="button" className="flex items-center gap-1 text-xs font-medium" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
        <span className={tone}>{label}</span> <span className="text-muted-foreground">{items.length}</span>
      </button>
      {open && <ul className="pl-5 text-xs space-y-0.5 font-mono">{items.map((i) => <li key={i}>{i}</li>)}</ul>}
    </div>
  );
}

function RoundView({ round, codes, onSelectRecord, onOpen }: {
  round: Round; codes: Map<string, { code: string | null; id: string }>; onSelectRecord: (id: string) => void; onOpen?: () => void;
}) {
  const order = { failed: 0, unverified: 1, passed: 2 } as const;
  const findings = [...round.findings].sort((a, b) =>
    order[a.status] - order[b.status] || Number(b.is_blocking) - Number(a.is_blocking));
  const count = (s: string) => round.findings.filter((f) => f.status === s).length;
  return (
    <div className="space-y-2">
      <div className="text-xs text-muted-foreground">
        Round {round.round} · {round.model} ·{" "}
        {round.status === "completed"
          ? <><span className="text-green-400">{count("passed")} passed</span>, <span className="text-red-400">{count("failed")} failed</span>, <span className="text-amber-400">{count("unverified")} unverified</span></>
          : <span className="text-amber-400">didn't complete ({round.detail})</span>}
        {onOpen && (
          <button type="button" className="ml-2 inline-flex items-center gap-1 hover:text-foreground" onClick={onOpen}>
            <ScrollText className="size-3" /> transcript
          </button>
        )}
      </div>
      {round.summary && <p className="text-sm text-muted-foreground">{round.summary}</p>}
      <ul className="space-y-1.5">
        {findings.map((f) => <FindingItem key={f.issue_key} finding={f} codes={codes} onSelectRecord={onSelectRecord} />)}
      </ul>
    </div>
  );
}

function FindingItem({ finding: f, codes, onSelectRecord }: {
  finding: Finding; codes: Map<string, { code: string | null; id: string }>; onSelectRecord: (id: string) => void;
}) {
  const [open, setOpen] = useState(f.status !== "passed");
  const refs = [...f.requirement_ids, ...(f.question_ids ?? [])].map((id) => codes.get(id)).filter(Boolean) as { code: string | null; id: string }[];
  return (
    <li className="rounded-md border text-sm">
      <button type="button" className="w-full text-left p-2 flex items-start gap-2" onClick={() => setOpen(!open)}>
        <StatusIcon status={f.status} />
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-1.5">
            <span className="font-mono text-xs">{f.issue_key}</span>
            <Badge variant="outline" className="font-normal text-[11px] h-5">{humanize(f.check_type)}</Badge>
            {f.is_blocking && f.status !== "passed" && <Badge variant="destructive" className="h-5 text-[11px]">blocking</Badge>}
            {f.edited_by_person && <Badge variant="outline" className="h-5 text-[11px] gap-1 font-normal"><UserPen className="size-3" /> your edit</Badge>}
          </span>
          {!open && <span className="block text-xs text-muted-foreground truncate">{f.summary}</span>}
        </span>
      </button>
      {open && (
        <div className="px-2 pb-2 pl-8 space-y-1.5 text-xs">
          <p className="text-sm">{f.summary}</p>
          {f.impact && <p><span className="text-muted-foreground">Impact: </span>{f.impact}</p>}
          {f.recommended_action && <p><span className="text-muted-foreground">Recommended: </span>{f.recommended_action}</p>}
          {f.edited_by_person && f.status !== "passed" && (
            <p className="text-muted-foreground">A person edited these elements, so the Architect asks instead of changing them.</p>
          )}
          {f.elements.length > 0 && (
            <div className="flex flex-wrap gap-1">{f.elements.map((e) => <span key={e} className="font-mono rounded border px-1">{e}</span>)}</div>
          )}
          {refs.length > 0 && (
            <div className="flex flex-wrap gap-1 items-center">
              <span className="text-muted-foreground">Concerns</span>
              {refs.map((r) => (
                <button key={r.id} type="button" onClick={() => onSelectRecord(r.id)}
                  className="font-mono rounded border px-1 hover:bg-accent">{r.code}</button>
              ))}
            </div>
          )}
          <Evidence items={f.evidence} />
        </div>
      )}
    </li>
  );
}

function Evidence({ items }: { items: Finding["evidence"] }) {
  if (items.length === 0) return <p className="text-amber-400">No evidence: unverified.</p>;
  return (
    <ul className="space-y-0.5 text-muted-foreground">
      {items.map((e, i) => (
        <li key={i} className="truncate">
          {e.kind === "source" && <>Source check <span className="font-mono">{e.tool}</span> · <span className="font-mono">{e.evidence_id?.slice(0, 8)}</span></>}
          {e.kind === "document" && <>Document: {e.citation}</>}
          {e.kind === "internal_check" && <>Internal check <span className="font-mono">{e.name}</span></>}
        </li>
      ))}
    </ul>
  );
}

function EarlierRounds({ rounds, attempts }: { rounds: Round[]; attempts: Record<string, number> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="text-xs">
      <button type="button" className="flex items-center gap-1 text-muted-foreground hover:text-foreground" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
        {rounds.length} earlier round{rounds.length > 1 ? "s" : ""} · corrections per issue
      </button>
      {open && (
        <div className="mt-1 space-y-2 pl-4">
          {rounds.map((r) => (
            <div key={r.round}>
              <div className="text-muted-foreground">Round {r.round}: {r.status}</div>
              <ul className="pl-3">
                {r.findings.filter((f) => f.status !== "passed").map((f) => (
                  <li key={f.issue_key}><span className={STATUS_TONE[f.status]}>{f.status}</span> <span className="font-mono">{f.issue_key}</span></li>
                ))}
              </ul>
            </div>
          ))}
          {Object.keys(attempts).length > 0 && (
            <div className="text-muted-foreground">
              Correction attempts: {Object.entries(attempts).map(([k, n]) => `${k} ×${n}`).join(", ")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function StatusIcon({ status }: { status: string }) {
  if (status === "passed") return <CheckCircle2 className="size-4 text-green-400 shrink-0 mt-0.5" />;
  if (status === "failed") return <XCircle className="size-4 text-red-400 shrink-0 mt-0.5" />;
  return <CircleHelp className="size-4 text-amber-400 shrink-0 mt-0.5" />;
}
