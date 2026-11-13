import { Check, X } from "lucide-react";
import type { Operation, Snapshot } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { humanize } from "@/lib/labels";
import { cn } from "@/lib/utils";

/**
 * Read-only views of the companion registers (P1). In P3/P4 the agents write these; editing
 * and confirmation flows come with them.
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
      <button type="button" onClick={onClick} className="w-full text-left p-2.5 pb-1.5 text-sm space-y-1.5">
        <div className="text-xs font-mono text-muted-foreground">{code}</div>
        {children}
      </button>
      {actions && <div className="flex gap-1.5 px-2.5 pb-2">{actions}</div>}
    </li>
  );
}

type Write = (ops: Operation[], o?: { reason?: string; success?: string }) => Promise<unknown>;

/** Confirm or reject a meaning. Only people with approval rights get these; agents never can. */
function Decide({ id, seq, code, write }: { id: string; seq: number; code: string | null; write: Write }) {
  const decide = (status: "confirmed" | "rejected") =>
    write([{ op: "update", id, expected_seq: seq, data: { confirmation_status: status } }],
      { success: `${code} ${status}`, reason: `${status === "confirmed" ? "Confirmed" : "Rejected"} by a person` });
  return (
    <>
      <Button size="sm" variant="outline" className="h-7" onClick={() => decide("confirmed")}><Check className="size-3.5" /> Confirm</Button>
      <Button size="sm" variant="ghost" className="h-7 text-muted-foreground" onClick={() => decide("rejected")}><X className="size-3.5" /> Reject</Button>
    </>
  );
}

export function RegistersPanel({
  snapshot,
  onSelectRecord,
  canApprove = false,
  write,
}: {
  snapshot: Snapshot;
  onSelectRecord: (id: string) => void;
  canApprove?: boolean;
  write?: Write;
}) {
  const decide = (r: { id: string; seq: number; code: string | null; data: { confirmation_status?: string } }) =>
    canApprove && write && (r.data.confirmation_status ?? "unconfirmed") === "unconfirmed"
      ? <Decide id={r.id} seq={r.seq} code={r.code} write={write} />
      : undefined;
  return (
    <div className="space-y-6">
      <Section title="Requirements" count={snapshot.requirements.length}>
        {snapshot.requirements.map((r) => (
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
          </Item>
        ))}
      </Section>

      <Section title="Business definitions" count={snapshot.definitions.length}>
        {snapshot.definitions.map((d) => (
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

      <Section title="Questions & conflicts" count={snapshot.questions.length}>
        {snapshot.questions.map((q) => (
          <Item key={q.id} code={q.code} onClick={() => onSelectRecord(q.id)}>
            <div>{q.data.description}</div>
            <div className="flex flex-wrap gap-1.5">
              <Status value={q.data.status} />
              <Badge variant="outline" className="font-normal">{humanize(q.data.kind)}</Badge>
              {q.data.is_blocking && <Badge variant="destructive">blocking</Badge>}
            </div>
          </Item>
        ))}
      </Section>

      <Section title="Decisions & assumptions" count={snapshot.decisions.length}>
        {snapshot.decisions.map((d) => (
          <Item key={d.id} code={d.code} onClick={() => onSelectRecord(d.id)}>
            <div>{d.data.statement}</div>
            {d.data.rationale && <div className="text-muted-foreground text-xs">{d.data.rationale}</div>}
            <div className="flex flex-wrap gap-1.5">
              <Status value={d.data.status} />
              <Badge variant="outline" className="font-normal">{humanize(d.data.kind)}</Badge>
            </div>
          </Item>
        ))}
      </Section>

      <Section title="Validation checks" count={snapshot.checks.length}>
        {snapshot.checks.map((c) => (
          <Item key={c.id} code={c.code} onClick={() => onSelectRecord(c.id)}>
            <div>{c.data.summary}</div>
            <div className="flex flex-wrap gap-1.5">
              <Status value={c.data.status} />
              <Badge variant="outline" className="font-normal">{humanize(c.data.check_type)}</Badge>
            </div>
          </Item>
        ))}
      </Section>
    </div>
  );
}
