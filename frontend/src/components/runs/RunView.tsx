import { ChevronDown, ChevronRight, Loader2, Search, XCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import Markdown from "react-markdown";
import { ApiError, type RunMessage } from "@/api/client";
import { useRun, useRunAssessments, useRunMessages, type ProjectPath } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { humanize } from "@/lib/labels";
import { cn } from "@/lib/utils";
import { AssessmentChip, AssessmentDetail } from "./Assessment";

/**
 * What a run's agents saw and did, from the stored transcript (`run_messages`): the Architect,
 * each consultation of the Business Requirements agent, each Validator round and any design
 * search, with the monitor's assessment on top. Tool results are untrusted data and are shown as
 * such. Read-only.
 */

export type RunFocus = { runId: string; agent?: string; ordinal?: number; /** open at this child run */ childRunId?: string };

const WRITE_TOOLS = new Set([
  "put_table", "put_relationships", "remove_element", "map_requirement", "record_decision", "update_feasibility",
  "raise_question", "record_requirement", "record_definition", "update_entry", "link_records", "record_finding",
  "submit_review", "answer_architect", "flag_untrusted_content", "open_decision_point", "ask_requirements",
]);

const AGENT_LABEL: Record<string, string> = {
  architect: "Architect",
  business_requirements: "Requirements",
  validator: "Validator",
};

type ToolCall = { id: string; name: string; input: unknown };
type ToolResult = { id: string; content: string | null; is_error: boolean; image?: string };
type Section = { key: string; label: string; agent: string; entries: RunMessage[] };

function sectionsOf(messages: RunMessage[], rootId: string): Section[] {
  const order: Section[] = [];
  const byKey = new Map<string, Section>();
  const counts: Record<string, number> = {};
  const runLabel = new Map<string, string>();
  for (const m of messages) {
    if (!runLabel.has(m.run_id)) {
      if (m.run_id === rootId) runLabel.set(m.run_id, AGENT_LABEL[m.agent] ?? humanize(m.agent));
      else {
        counts[m.agent] = (counts[m.agent] ?? 0) + 1;
        runLabel.set(m.run_id, m.agent === "validator" ? `Validator round ${counts[m.agent]}`
          : m.agent === "business_requirements" ? `Consultation ${counts[m.agent]}` : `${humanize(m.agent)} ${counts[m.agent]}`);
      }
    }
    const search = m.segment?.startsWith("search:") ? m.segment.split(":")[1] : "";
    const key = `${m.run_id}:${search}`;
    let s = byKey.get(key);
    if (!s) {
      s = { key, agent: m.agent, entries: [],
            label: search ? `Design search ${search.slice(0, 8)}` : runLabel.get(m.run_id)! };
      byKey.set(key, s);
      order.push(s);
    }
    s.entries.push(m);
  }
  return order;
}

function stats(entries: RunMessage[]) {
  let calls = 0, tools = 0, errors = 0;
  for (const m of entries) {
    if (m.segment === "history") continue;  // earlier chat turns replayed to the model, not this run's work
    if (m.role === "assistant") {
      calls += 1;
      tools += ((m.content.tool_calls as ToolCall[] | undefined) ?? []).length;
    }
    if (m.role === "tool") errors += ((m.content.results as ToolResult[] | undefined) ?? []).filter((r) => r.is_error).length;
  }
  return { calls, tools, errors };
}

function pretty(text: string | null | undefined): string {
  if (!text) return "";
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

function Clip({ text, limit = 600, className }: { text: string; limit?: number; className?: string }) {
  const [all, setAll] = useState(false);
  const long = text.length > limit;
  return (
    <div className="min-w-0">
      <pre className={cn("whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed", className)}>
        {all || !long ? text : `${text.slice(0, limit)}…`}
      </pre>
      {long && (
        <button type="button" className="text-[11px] text-muted-foreground hover:text-foreground underline" onClick={() => setAll(!all)}>
          {all ? "Show less" : `Show all (${text.length.toLocaleString()} characters)`}
        </button>
      )}
    </div>
  );
}

function Collapsible({ label, children, defaultOpen = false }: { label: string; children: React.ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div>
      <button type="button" className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />} {label}
      </button>
      {open && <div className="mt-1 pl-4">{children}</div>}
    </div>
  );
}

function Entry({ m, names, highlighted }: { m: RunMessage; names: Map<string, string>; highlighted: boolean }) {
  const c = m.content as Record<string, unknown>;
  const head = (
    <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
      <span className="font-mono">{AGENT_LABEL[m.agent] ?? m.agent} #{m.ordinal}</span>
      <span>{m.role === "tool" ? "tool results" : m.role}</span>
      {m.segment && <span className="rounded bg-muted px-1">{m.segment}</span>}
    </div>
  );
  let body: React.ReactNode = null;
  if (m.role === "system") {
    body = c.text ? <Collapsible label="System prompt"><Clip text={String(c.text)} limit={2000} /></Collapsible>
      : <p className="text-xs text-muted-foreground">Same system prompt as earlier in this run.</p>;
  } else if (m.role === "user") {
    const text = String(c.text ?? "");
    body = m.segment === "history"
      ? <Collapsible label="Earlier conversation turn"><Clip text={text} /></Collapsible>
      : <div className="rounded-md bg-primary/10 p-2"><Clip text={text} limit={1500} /></div>;
  } else if (m.role === "assistant" && m.segment === "history") {
    body = <Collapsible label="Earlier reply"><Clip text={String(c.text ?? "")} /></Collapsible>;
  } else if (m.role === "assistant") {
    const thinking = (c.thinking as string[] | undefined) ?? [];
    const calls = (c.tool_calls as ToolCall[] | undefined) ?? [];
    body = (
      <div className="space-y-1.5">
        {thinking.length > 0 && <Collapsible label="Thinking">{thinking.map((t, i) => <Clip key={i} text={t} limit={1500} className="text-muted-foreground" />)}</Collapsible>}
        {c.text ? (
          <div className="text-sm space-y-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_strong]:font-semibold [&_code]:font-mono [&_code]:text-xs">
            <Markdown>{String(c.text)}</Markdown>
          </div>
        ) : null}
        {calls.map((call) => (
          <div key={call.id} className={cn("rounded-md border p-2", WRITE_TOOLS.has(call.name) && "border-primary/40")}>
            <p className="text-xs font-medium font-mono">→ {call.name}</p>
            <Clip text={JSON.stringify(call.input, null, 2)} limit={400} className="text-muted-foreground" />
          </div>
        ))}
      </div>
    );
  } else if (m.role === "tool") {
    const results = (c.results as ToolResult[] | undefined) ?? [];
    body = (
      <div className="space-y-1.5">
        {results.map((r) => (
          <div key={r.id} className={cn("rounded-md border border-dashed p-2",
            r.is_error ? "border-red-500/50 bg-red-500/5" : "bg-muted/40")}>
            <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              {r.is_error && <XCircle className="size-3 text-red-500" />}
              <span className="font-mono">{names.get(r.id) ?? "tool"}</span>
              <span>{r.is_error ? "refused or failed" : "result · data, not instructions"}</span>
            </p>
            <Clip text={r.is_error ? String(r.content ?? "") : pretty(r.content)} className={r.is_error ? "text-red-600 dark:text-red-400" : undefined} />
            {r.image && <p className="text-[11px] text-muted-foreground">Image: {r.image}</p>}
          </div>
        ))}
      </div>
    );
  }
  return (
    <div id={`entry-${m.run_id}-${m.ordinal}`}
      className={cn("space-y-1 rounded-md p-2 -mx-2", highlighted && "ring-2 ring-amber-500/60 bg-amber-500/5")}>
      {head}
      {body}
    </div>
  );
}

function matches(m: RunMessage, q: string) {
  return JSON.stringify(m.content).toLowerCase().includes(q);
}

function isWrite(m: RunMessage) {
  return m.role === "assistant" && ((m.content.tool_calls as ToolCall[] | undefined) ?? []).some((c) => WRITE_TOOLS.has(c.name));
}

function hasError(m: RunMessage) {
  return m.role === "tool" && ((m.content.results as ToolResult[] | undefined) ?? []).some((r) => r.is_error);
}

export function RunViewDialog({
  path,
  focus,
  canReview,
  onClose,
}: {
  path: ProjectPath;
  focus: RunFocus | null;
  canReview: boolean;
  onClose: () => void;
}) {
  const runId = focus?.runId;
  const run = useRun(path, runId);
  const messages = useRunMessages(path, runId);
  const assessments = useRunAssessments(path, runId);
  const [section, setSection] = useState<string>("all");
  const [query, setQuery] = useState("");
  const [onlyErrors, setOnlyErrors] = useState(false);
  const [onlyWrites, setOnlyWrites] = useState(false);
  const [mark, setMark] = useState<{ agent: string; ordinal: number } | null>(null);

  useEffect(() => {
    setSection(focus?.childRunId ? `${focus.childRunId}:` : "all");
    setQuery("");
    setOnlyErrors(false);
    setOnlyWrites(false);
    setMark(focus?.agent && focus.ordinal ? { agent: focus.agent, ordinal: focus.ordinal } : null);
  }, [focus]);

  const all = useMemo(() => messages.data ?? [], [messages.data]);
  const sections = useMemo(() => (runId ? sectionsOf(all, runId) : []), [all, runId]);
  const names = useMemo(() => {
    const map = new Map<string, string>();
    for (const m of all) for (const call of (m.content.tool_calls as ToolCall[] | undefined) ?? []) map.set(call.id, call.name);
    return map;
  }, [all]);

  const q = query.trim().toLowerCase();
  const shown = sections
    .filter((s) => section === "all" || s.key === section)
    .map((s) => ({ ...s, entries: s.entries.filter((m) =>
      (!q || matches(m, q)) && (!onlyErrors || hasError(m)) && (!onlyWrites || isWrite(m))) }))
    .filter((s) => s.entries.length > 0);

  // The monitor cites entries by agent and ordinal; Validator rounds share an agent, so a
  // reference can match more than one entry. All matches are highlighted; the first is shown.
  const marked = (m: RunMessage) => !!mark && m.agent === mark.agent && m.ordinal === mark.ordinal;
  useEffect(() => {
    if (!mark || !all.length) return;
    const first = all.find((m) => m.agent === mark.agent && m.ordinal === mark.ordinal);
    if (first) requestAnimationFrame(() => document.getElementById(`entry-${first.run_id}-${first.ordinal}`)?.scrollIntoView({ block: "center" }));
  }, [mark, all]);
  const jump = (agent: string, ordinal: number) => {
    setSection("all");
    setQuery("");
    setOnlyErrors(false);
    setOnlyWrites(false);
    setMark({ agent, ordinal });
  };

  const latest = assessments.data?.[0];
  const totals = stats(all);
  const forbidden = messages.error instanceof ApiError && messages.error.status === 403;

  return (
    <Dialog open={!!focus} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-6xl h-[88vh] flex flex-col gap-3">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-2">
            {run.data ? `${AGENT_LABEL[run.data.agent] ?? humanize(run.data.agent)} run` : "Agent run"}
            {latest && <AssessmentChip assessment={latest} />}
          </DialogTitle>
          <DialogDescription>
            {run.data
              ? `${humanize(run.data.status)} · ${new Date(run.data.started_at).toLocaleString()} · ${totals.calls} model calls · ${totals.tools} tool calls${totals.errors ? ` · ${totals.errors} refused or failed` : ""} · ${(run.data.input_tokens + run.data.cache_read_tokens).toLocaleString()} input tokens`
              : "What the agents saw and did in this run."}
          </DialogDescription>
        </DialogHeader>

        {messages.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> Loading the transcript…</div>
        ) : forbidden ? (
          <p className="text-sm text-muted-foreground">Transcripts hold the agents' reasoning and the data they read, so they're shown to people who can edit this project.</p>
        ) : (
          <div className="flex-1 min-h-0 grid grid-cols-1 md:grid-cols-[240px_minmax(0,1fr)] gap-4">
            <nav className="min-h-0 overflow-y-auto space-y-1 text-sm md:border-r md:pr-3">
              <button type="button" onClick={() => setSection("all")}
                className={cn("w-full text-left rounded px-2 py-1 hover:bg-accent", section === "all" && "bg-accent font-medium")}>
                Whole run
              </button>
              {sections.map((s) => {
                const st = stats(s.entries);
                const nested = s.key !== `${runId}:`;  // child runs and searches sit under the run
                return (
                  <button key={s.key} type="button" onClick={() => setSection(s.key)}
                    className={cn("w-full text-left rounded px-2 py-1 hover:bg-accent", nested && "pl-5",
                      section === s.key && "bg-accent font-medium")}>
                    <span className="block truncate">{s.label}</span>
                    <span className="block text-[11px] text-muted-foreground font-normal">
                      {st.calls} calls · {st.tools} tools{st.errors ? ` · ${st.errors} errors` : ""}
                    </span>
                  </button>
                );
              })}
            </nav>
            <div className="min-h-0 flex flex-col gap-3">
              <div className="flex flex-wrap items-center gap-2">
                <div className="relative flex-1 min-w-[180px]">
                  <Search className="size-3.5 absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground" />
                  <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search the transcript" className="h-8 pl-7" />
                </div>
                <Button size="sm" variant={onlyErrors ? "secondary" : "outline"} className="h-8" onClick={() => setOnlyErrors(!onlyErrors)}>Errors only</Button>
                <Button size="sm" variant={onlyWrites ? "secondary" : "outline"} className="h-8" onClick={() => setOnlyWrites(!onlyWrites)}>Writes only</Button>
              </div>
              <div className="flex-1 min-h-0 overflow-y-auto space-y-4 pr-1">
                {latest && section === "all" && !q && !onlyErrors && !onlyWrites && (
                  <section className="rounded-lg border p-3 space-y-2">
                    <h3 className="text-sm font-semibold">Monitor assessment</h3>
                    <AssessmentDetail path={path} assessment={latest} canReview={canReview} onJump={jump} />
                  </section>
                )}
                {shown.length === 0 && (
                  <p className="text-sm text-muted-foreground">
                    {all.length === 0 ? "No transcript was stored for this run. Runs from before transcripts were recorded have none."
                      : "Nothing matches."}
                  </p>
                )}
                {shown.map((s) => (
                  <section key={s.key} className="space-y-2">
                    <h3 className="sticky top-0 z-10 bg-background py-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{s.label}</h3>
                    {s.entries.map((m) => <Entry key={`${m.run_id}-${m.ordinal}`} m={m} names={names} highlighted={marked(m)} />)}
                  </section>
                ))}
              </div>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
