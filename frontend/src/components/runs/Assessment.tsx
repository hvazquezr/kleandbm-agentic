import { AlertTriangle, Loader2, ShieldAlert, ShieldCheck, ShieldQuestion } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import type { Assessment } from "@/api/client";
import { useReviewAssessment, type ProjectPath } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { humanize } from "@/lib/labels";
import { cn } from "@/lib/utils";

/**
 * The transcript monitor's reading of a run: triage for people, never a gate. A flagged run is
 * worth a reviewer's look; a low score isn't proof of a clean run.
 */

type Tone = "flagged" | "glance" | "clear" | "waiting" | "none";

export function assessmentTone(a: Assessment | undefined): Tone {
  if (!a) return "none";
  if (a.status === "pending") return "waiting";
  if (a.status !== "completed" || a.score === null) return "none";
  if (a.flagged) return "flagged";
  return a.score > 20 ? "glance" : "clear";
}

const LABEL: Record<Tone, string> = {
  flagged: "Flagged",
  glance: "Worth a look",
  clear: "No concerns",
  waiting: "Monitor reading…",
  none: "Not assessed",
};

const STYLE: Record<Tone, string> = {
  flagged: "border-red-500/40 bg-red-500/10 text-red-600 dark:text-red-400",
  glance: "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400",
  clear: "border-border bg-muted text-muted-foreground",
  waiting: "border-border bg-muted text-muted-foreground",
  none: "border-border bg-muted text-muted-foreground",
};

function verdictLabel(v: string) {
  return v === "false_alarm" ? "false alarm" : "real concern";
}

/** A small chip with the monitor's verdict for a run. */
export function AssessmentChip({ assessment, onClick }: { assessment: Assessment | undefined; onClick?: () => void }) {
  const tone = assessmentTone(assessment);
  const Icon = tone === "flagged" ? ShieldAlert : tone === "glance" ? ShieldQuestion : tone === "waiting" ? Loader2 : ShieldCheck;
  const review = assessment?.reviews[0];
  const title = assessment?.status === "skipped" ? "No monitor model is configured"
    : assessment?.status === "failed" ? `The monitor couldn't assess this run${assessment.detail ? `: ${assessment.detail}` : ""}`
    : assessment?.summary ?? "The transcript monitor scores how likely it is that an agent misbehaved";
  return (
    <button type="button" onClick={onClick} title={title}
      className={cn("inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-medium whitespace-nowrap", STYLE[tone],
        onClick && "hover:opacity-80")}>
      <Icon className={cn("size-3", tone === "waiting" && "animate-spin")} />
      {assessment?.status === "skipped" ? "No monitor" : assessment?.status === "failed" ? "Monitor failed" : LABEL[tone]}
      {assessment?.score !== null && assessment?.score !== undefined && <span className="tabular-nums">{assessment.score}</span>}
      {review && <span className="font-normal">· reviewed</span>}
    </button>
  );
}

/** Shown where a flagged run's work is used (its reply, the Review drawer). */
export function FlaggedBanner({ assessment, onOpen }: { assessment: Assessment | undefined; onOpen?: () => void }) {
  if (assessmentTone(assessment) !== "flagged" || !assessment) return null;
  const review = assessment.reviews[0];
  return (
    <div role="status" className="rounded-md border border-red-500/40 bg-red-500/10 p-2.5 text-xs space-y-1">
      <p className="flex items-center gap-1.5 font-medium text-red-600 dark:text-red-400">
        <AlertTriangle className="size-3.5" /> The transcript monitor flagged this run ({assessment.score}/100)
      </p>
      {assessment.summary && <p className="text-foreground/90">{assessment.summary}</p>}
      <div className="flex items-center gap-2">
        {review ? (
          <span className="text-muted-foreground">Reviewed as {verdictLabel(review.verdict)}{review.by_name && ` by ${review.by_name}`}.</span>
        ) : (
          <span className="text-muted-foreground">Not reviewed yet. Check the run before relying on its work.</span>
        )}
        {onOpen && <Button size="sm" variant="outline" className="h-6 px-2 text-xs ml-auto" onClick={onOpen}>Open the run</Button>}
      </div>
    </div>
  );
}

/** The full assessment, with links to the transcript entries it cites and the review form. */
export function AssessmentDetail({
  path,
  assessment,
  canReview,
  onJump,
}: {
  path: ProjectPath;
  assessment: Assessment;
  canReview: boolean;
  onJump: (agent: string, ordinal: number) => void;
}) {
  const review = useReviewAssessment(path);
  const [note, setNote] = useState("");
  const latest = assessment.reviews[0];
  const submit = async (verdict: "concern_confirmed" | "false_alarm") => {
    try {
      await review.mutateAsync({ runId: assessment.run_id, assessmentId: assessment.id, verdict, note });
      setNote("");
      toast.success("Review recorded");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't record the review");
    }
  };
  const Ref = ({ agent, ordinal }: { agent: string; ordinal: number }) => (
    <button type="button" className="font-mono text-[11px] rounded border px-1 hover:bg-accent" onClick={() => onJump(agent, ordinal)}>
      {humanize(agent)} #{ordinal}
    </button>
  );

  if (assessment.status !== "completed") {
    return (
      <p className="text-sm text-muted-foreground">
        {assessment.status === "pending" ? "The monitor is still reading this run."
          : assessment.status === "skipped" ? "No monitor model is configured, so this run wasn't assessed."
          : `The monitor couldn't assess this run${assessment.detail ? `: ${assessment.detail}` : "."}`}
      </p>
    );
  }
  return (
    <div className="space-y-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <AssessmentChip assessment={assessment} />
        <span className="text-xs text-muted-foreground">
          {assessment.monitor_model}{assessment.samples.length > 1 && ` · samples ${assessment.samples.join(", ")}`}
          {assessment.truncated && " · the transcript was shortened to fit"}
        </span>
      </div>
      {assessment.summary && <p>{assessment.summary}</p>}
      {assessment.concerns.length > 0 && (
        <ul className="space-y-2">
          {assessment.concerns.map((c, i) => (
            <li key={i} className="rounded-md border p-2 space-y-1">
              <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{humanize(c.kind)}</p>
              <p>{c.explanation}</p>
              {c.entries.length > 0 && <div className="flex flex-wrap gap-1">{c.entries.map((e, j) => <Ref key={j} {...e} />)}</div>}
            </li>
          ))}
        </ul>
      )}
      {assessment.untrusted_instructions.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs font-medium text-muted-foreground">Instructions found in untrusted content</p>
          <ul className="space-y-1">
            {assessment.untrusted_instructions.map((u, i) => (
              <li key={i} className="flex items-start gap-2 text-xs">
                <Ref agent={u.agent} ordinal={u.ordinal} />
                <span className="flex-1 min-w-0 italic break-words">“{u.quote}”</span>
                <span className={cn("shrink-0 font-medium", u.followed ? "text-red-600 dark:text-red-400" : "text-muted-foreground")}>
                  {u.followed ? "followed" : "ignored"}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="rounded-md border p-2 space-y-2">
        {latest ? (
          <p className="text-xs">
            Reviewed as <span className="font-medium">{verdictLabel(latest.verdict)}</span>
            {latest.by_name && <> by {latest.by_name}</>} on {new Date(latest.created_at).toLocaleString()}
            {latest.note && <>: {latest.note}</>}
          </p>
        ) : (
          <p className="text-xs text-muted-foreground">No one has reviewed this assessment yet.</p>
        )}
        {canReview && (
          <>
            <Textarea rows={2} value={note} placeholder="What you checked (optional)" onChange={(e) => setNote(e.target.value)} />
            <div className="flex gap-2">
              <Button size="sm" variant="outline" disabled={review.isPending} onClick={() => submit("concern_confirmed")}>Real concern</Button>
              <Button size="sm" variant="outline" disabled={review.isPending} onClick={() => submit("false_alarm")}>False alarm</Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
