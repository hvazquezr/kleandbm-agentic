import { Check, MessageSquareReply, X } from "lucide-react";
import { useMemo } from "react";
import type { Operation, Snapshot } from "@/api/client";
import { CheckLine, RefChip } from "@/components/design/ElementTrace";
import { buildTrace } from "@/lib/trace";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { humanize } from "@/lib/labels";
import { cn } from "@/lib/utils";

/**
 * The companion registers, with traceability: what each requirement maps to in the model and
 * which validation checks cover it, and what each check covers. People with approval rights
 * confirm or reject meanings here; agents never can.
 */

const TONE: Record<string, string> = {
  confirmed: "bg-green-500/15 text-green-400 border-green-500/30",
  fully_supported: "bg-green-500/15 text-green-400 border-green-500/30",
  passed: "bg-green-500/15 text-green-400 border-green-500/30",
  resolved: "bg-green-500/15 text-green-400 border-green-500/30",
  partially_supported: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  unverified: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  proposed: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  open: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  unsupported: "bg-red-500/15 text-red-400 border-red-500/30",
  failed: "bg-red-500/15 text-red-400 border-red-500/30",
  rejected: "bg-red-500/15 text-red-400 border-red-500/30",
};

function Status({ value }: { value: string | null | undefined }) {
  if (!value) return null;
  return <Badge variant="outline" className={cn("font-normal", TONE[value])}>{humanize(value)}</Badge>;
}

function Section({ title, count, children }: { title: string; count: number; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold flex items-center gap-2">
        {title} <span className="text-muted-foreground font-normal">{count}</span>
      </h3>
      {count === 0 ? <p className="text-sm text-muted-foreground">None recorded.</p> : <ul className="space-y-2">{children}</ul>}
    </section>
  );
}

function Item({
  code,
  children,
  onClick,
  actions,
}: {
  code: string | null;
  children: React.ReactNode;
  onClick?: () => void;
  actions?: React.ReactNode;
}) {
  return (
    <li className="rounded-md border hover:bg-accent/30">
      {/* A div, not a button: items contain their own buttons (trace chips). */}
      <div role="button" tabIndex={0} onClick={onClick}
        onKeyDown={(e) => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); onClick?.(); } }}
        className="w-full text-left p-2.5 pb-1.5 text-sm space-y-1.5 cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-md">
        <div className="text-xs font-mono text-muted-foreground">{code}</div>
        {children}
      </div>
      {actions && <div className="flex gap-1.5 px-2.5 pb-2">{actions}</div>}
    </li>
  );
}

type Write = (ops: Operation[], o?: { reason?: string; success?: string }) => Promise<unknown>;

/** Confirm or reject an entry. Meanings need approval rights and decisions need edit rights (the
 *  server enforces both); agents never can. */
function Decide({ id, seq, code, write, field = "confirmation_status" }: {
  id: string; seq: number; code: string | null; write: Write; field?: "confirmation_status" | "status";
}) {
  const decide = (status: "confirmed" | "rejected") =>
    write([{ op: "update", id, expected_seq: seq, data: { [field]: status } }],
      { success: `${code} ${status}`, reason: `${status === "confirmed" ? "Confirmed" : "Rejected"} by a person` });
  return (
    <>
      <Button size="sm" variant="outline" className="h-7" onClick={() => decide("confirmed")}><Check className="size-3.5" /> Confirm</Button>
      <Button size="sm" variant="ghost" className="h-7 text-muted-foreground" onClick={() => decide("rejected")}><X className="size-3.5" /> Reject</Button>
    </>
  );
}

/** Register entries in code order (REQ-001, REQ-002, … REQ-010, numbers compared as numbers);
 *  entries without a code go last. */
function byCode<T extends { code: string | null }>(items: T[]): T[] {
  return [...items].sort((a, b) =>
    a.code === null ? (b.code === null ? 0 : 1) : b.code === null ? -1
      : a.code.localeCompare(b.code, undefined, { numeric: true }));
}

export type RegisterKey = "requirements" | "definitions" | "questions" | "decisions" | "checks";

export function RegistersPanel({
  snapshot,
  onSelectRecord,
  canApprove = false,
  write,
  only,
  onOpenReview,
  canEdit = false,
  onAnswer,
}: {
  snapshot: Snapshot;
  onSelectRecord: (id: string) => void;
  canApprove?: boolean;
  /** Editors confirm or reject design decisions and answer questions. */
  canEdit?: boolean;
  /** Answer an open question through the Requirements agent. */
  onAnswer?: (question: { id: string; code: string | null }) => void;
  write?: Write;
  /** Show one register instead of all of them. */
  only?: RegisterKey;
  /** Open the Review tab, where decision-point questions are settled. */
  onOpenReview?: () => void;
}) {
  const show = (k: RegisterKey) => !only || only === k;
  const trace = useMemo(() => buildTrace(snapshot), [snapshot]);
  const decide = (r: { id: string; seq: number; code: string | null; data: { confirmation_status?: string } }) =>
    canApprove && write && (r.data.confirmation_status ?? "unconfirmed") === "unconfirmed"
      ? <Decide id={r.id} seq={r.seq} code={r.code} write={write} />
      : undefined;
  return (
    <div className="space-y-6">
      {show("requirements") && (
      <Section title="Requirements" count={snapshot.requirements.length}>
        {byCode(snapshot.requirements).map((r) => (
          <Item key={r.id} code={r.code} onClick={() => onSelectRecord(r.id)} actions={decide(r)}>
            <div>{r.data.statement}</div>
            <div className="flex flex-wrap gap-1.5">
              <Status value={r.data.confirmation_status} />
              <Status value={r.data.feasibility_status} />
              {r.data.is_proposed_alternative && <Badge variant="outline">proposed alternative</Badge>}
            </div>
            <div className="text-xs text-muted-foreground">
              Origin: {humanize(r.data.origin_kind)}{r.data.origin_ref ? ` — ${r.data.origin_ref}` : ""}
            </div>
            {r.data.feasibility_notes && <div className="text-xs text-muted-foreground">Feasibility: {r.data.feasibility_notes}</div>}
            <UsesDefinitions requirement={r} snapshot={snapshot} onSelect={onSelectRecord}
              write={canEdit ? write : undefined} />
            <Traced label="Mapped to" ids={trace.mappedTo(r.id)} trace={trace} onSelect={onSelectRecord}
              empty={r.data.feasibility_status === "unsupported" ? undefined : "Not mapped to the model yet"} />
            {trace.checksFor([r.id]).map((c) => <CheckLine key={c.id} check={c} onSelect={onSelectRecord} />)}
          </Item>
        ))}
      </Section>
      )}

      {show("definitions") && (
      <Section title="Business definitions" count={snapshot.definitions.length}>
        {byCode(snapshot.definitions).map((d) => (
          <Item key={d.id} code={d.code} onClick={() => onSelectRecord(d.id)} actions={decide(d)}>
            <div className="font-medium">{d.data.term} <span className="text-xs text-muted-foreground">({humanize(d.data.kind)})</span></div>
            {d.data.definition ? <div className="text-muted-foreground">{d.data.definition}</div>
              : <div className="text-amber-500 text-xs">Meaning not yet defined</div>}
            {d.data.metric?.formula && <div className="font-mono text-xs">{d.data.metric.formula}</div>}
            <div className="flex flex-wrap gap-1.5">
              <Status value={d.data.confirmation_status} />
              <Badge variant="outline" className="font-normal">{humanize(d.data.provenance)}</Badge>
            </div>
          </Item>
        ))}
      </Section>
      )}

      {show("questions") && (
      <Section title="Questions & conflicts" count={snapshot.questions.length}>
        {byCode(snapshot.questions).map((q) => (
          <Item key={q.id} code={q.code} onClick={() => onSelectRecord(q.id)}
            actions={q.data.kind !== "decision_point" && q.data.status === "open" && canEdit && onAnswer ? (
              <Button size="sm" variant="outline" className="h-7" onClick={() => onAnswer({ id: q.id, code: q.code })}>
                <MessageSquareReply className="size-3.5" /> Answer
              </Button>
            ) : q.data.kind === "decision_point" && q.data.status === "open" ? (
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <span>Settled on the Review tab, by accepting or rejecting the design the search left. Answering it here or in chat doesn't clear the blocker.</span>
                {onOpenReview && (
                  <Button size="sm" variant="outline" className="h-7" onClick={onOpenReview}>Open Review</Button>
                )}
              </div>
            ) : undefined}>
            <div>{q.data.description}</div>
            <div className="flex flex-wrap gap-1.5">
              <Status value={q.data.status} />
              <Badge variant="outline" className="font-normal">{humanize(q.data.kind)}</Badge>
              {q.data.is_blocking && <Badge variant="destructive">blocking</Badge>}
            </div>
            <QuestionLinks id={q.id} trace={trace} onSelect={onSelectRecord} />
          </Item>
        ))}
      </Section>
      )}

      {show("decisions") && (
      <Section title="Decisions & assumptions" count={snapshot.decisions.length}>
        {byCode(snapshot.decisions).map((d) => (
          <Item key={d.id} code={d.code} onClick={() => onSelectRecord(d.id)}
            actions={canEdit && write && d.data.status === "proposed"
              ? <Decide id={d.id} seq={d.seq} code={d.code} write={write} field="status" /> : undefined}>
            <div>{d.data.statement}</div>
            {d.data.rationale && <div className="text-muted-foreground text-xs">{d.data.rationale}</div>}
            <div className="flex flex-wrap gap-1.5">
              <Status value={d.data.status} />
              <Badge variant="outline" className="font-normal">{humanize(d.data.kind)}</Badge>
            </div>
          </Item>
        ))}
      </Section>
      )}

      {show("checks") && (
      <Section title="Validation checks" count={snapshot.checks.length}>
        {byCode(snapshot.checks).map((c) => (
          <Item key={c.id} code={c.code} onClick={() => onSelectRecord(c.id)}>
            <div>{c.data.summary}</div>
            <div className="flex flex-wrap gap-1.5">
              <Status value={c.data.status} />
              <Badge variant="outline" className="font-normal">{humanize(c.data.check_type)}</Badge>
              {c.data.is_blocking && c.data.status !== "passed" && <Badge variant="destructive">blocking</Badge>}
              {c.data.issue_key && <span className="font-mono text-[11px] text-muted-foreground self-center">{c.data.issue_key}</span>}
            </div>
            {c.data.recommended_action && c.data.status !== "passed" && (
              <div className="text-xs text-muted-foreground">Recommended: {c.data.recommended_action}</div>
            )}
            <Traced label="Covers" ids={trace.checked(c.id)} trace={trace} onSelect={onSelectRecord} />
          </Item>
        ))}
      </Section>
      )}
    </div>
  );
}

/** What a question is tied to: the requirements and definitions it concerns, the decisions and
 *  assumptions standing in for its answer, and the validation checks that name it. These are
 *  what to revisit once it's answered. */
function QuestionLinks({ id, trace, onSelect }: {
  id: string; trace: ReturnType<typeof buildTrace>; onSelect: (id: string) => void;
}) {
  const ordered = (ids: string[]) =>
    [...new Set(ids)].sort((a, b) => trace.label(a).localeCompare(trace.label(b), undefined, { numeric: true }));
  const concerns = ordered(trace.linksFrom(id, "concerns"));
  const of = (kind: string) => concerns.filter((c) => trace.kindOf(c) === kind);
  const other = concerns.filter((c) => !["requirement", "definition"].includes(trace.kindOf(c) ?? ""));
  return (
    <>
      <Traced label="Requirements" ids={of("requirement")} trace={trace} onSelect={onSelect} />
      <Traced label="Definitions" ids={of("definition")} trace={trace} onSelect={onSelect} />
      <Traced label="Model" ids={other} trace={trace} onSelect={onSelect} />
      <Traced label="Decisions" ids={ordered(trace.linksTo(id, "affects"))} trace={trace} onSelect={onSelect} />
      <Traced label="Checks" ids={ordered(trace.linksTo(id, "checks"))} trace={trace} onSelect={onSelect} />
    </>
  );
}

function Traced({ label, ids, trace, onSelect, empty }: {
  label: string; ids: string[]; trace: ReturnType<typeof buildTrace>; onSelect: (id: string) => void; empty?: string;
}) {
  if (ids.length === 0) return empty ? <div className="text-xs text-amber-500">{empty}</div> : null;
  return (
    <div className="flex flex-wrap items-center gap-1 text-xs">
      <span className="text-muted-foreground">{label}</span>
      {ids.map((id) => <RefChip key={id} id={id} trace={trace} onSelect={onSelect} />)}
    </div>
  );
}

/** The definitions a requirement uses, each removable by an editor: a rejected or replaced
 *  definition shouldn't stay linked, or the model keeps depending on it (acceptance refuses a
 *  rejected one in use). Removing sends an unlink changeset as the person. */
function UsesDefinitions({ requirement, snapshot, onSelect, write }: {
  requirement: { id: string; code: string | null };
  snapshot: Snapshot;
  onSelect: (id: string) => void;
  write?: Write;
}) {
  const defs = new Map(snapshot.definitions.map((d) => [d.id, d]));
  const used = snapshot.links
    .filter((l) => l.link_type === "uses_definition" && l.from_id === requirement.id && defs.has(l.to_id))
    .map((l) => ({ link: l, def: defs.get(l.to_id)! }));
  if (used.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1 text-xs">
      <span className="text-muted-foreground">Uses</span>
      {used.map(({ link, def }) => {
        const rejected = def.data.confirmation_status === "rejected";
        return (
          <span key={link.id} className={cn("inline-flex items-center gap-0.5 rounded border pl-1.5 pr-0.5 py-0.5",
            rejected ? "border-red-500/40 text-red-400" : "text-foreground")}>
            <button type="button" className="hover:underline" title={rejected ? "Rejected definition" : def.data.definition ?? undefined}
              onClick={(e) => { e.stopPropagation(); onSelect(def.id); }}>
              <span className="font-mono">{def.code}</span> {def.data.term}{rejected ? " (rejected)" : ""}
            </button>
            {write && (
              <button type="button" aria-label={`Stop using ${def.code}`} title={`${requirement.code} no longer uses ${def.code}`}
                className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground"
                onClick={(e) => {
                  e.stopPropagation();
                  void write([{ op: "unlink", link_id: link.id }],
                    { success: `${requirement.code} no longer uses ${def.code}`, reason: `Removed ${def.code} from ${requirement.code}` });
                }}>
                <X className="size-3" />
              </button>
            )}
          </span>
        );
      })}
    </div>
  );
}
