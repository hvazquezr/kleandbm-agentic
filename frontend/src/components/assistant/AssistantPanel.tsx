import { Bot, ChevronDown, ChevronRight, ClipboardList, DraftingCompass, FileText, Loader2, MessageSquareText, Paperclip, Route, ScrollText, Send, ShieldCheck, Sparkles, User, X, XCircle } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import Markdown from "react-markdown";
import { toast } from "sonner";
import { sendAgentMessage, uploadDocument, type AgentEvent, type TurnAgent } from "@/api/agent";
import { ApiError, type Assessment } from "@/api/client";
import {
  useCandidates,
  useConversationAssessments,
  useConversations,
  useCreateConversation,
  useDocuments,
  useInvalidateAfterAgent,
  useMessages,
  type AgentName,
  type ConversationAgent,
  type ProjectPath,
} from "@/api/hooks";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { AssessmentChip, FlaggedBanner } from "@/components/runs/Assessment";
import { cn } from "@/lib/utils";

type Attachment = { versionId: string; label: string; status: string };
type ToolActivity = {
  /** Text the agent wrote between steps; kept in order with its tool calls. */
  narration?: string;
  name: string;
  status: "started" | "ok" | "error";
  summary?: string;
  record?: { id: string; code: string; action?: string } | null;
  /** The subgraph that made the call, when the Architect consulted or was reviewed. */
  agent?: "business_requirements" | "validator" | "review";
};

/** The subgraph behind an event, if any: in a routed turn, an event whose agent is its own step's isn't one. */
function subagent(e: { agent?: string; step?: string }): "business_requirements" | "validator" | undefined {
  if (!e.agent || e.agent === e.step) return undefined;
  return e.agent === "business_requirements" || e.agent === "validator" ? e.agent : undefined;
}

const TOOL_LABELS: Record<string, string> = {
  list_documents: "Listing documents",
  read_document: "Reading a document",
  search_documents: "Searching documents",
  get_registers: "Checking the registers",
  record_requirement: "Recording a requirement",
  record_definition: "Recording a definition",
  update_entry: "Updating an entry",
  raise_question: "Raising a question",
  link_records: "Linking records",
  view_image: "Looking at an image",
  list_sources: "Listing data sources",
  source_list_tables: "Listing source tables",
  source_describe_table: "Describing a source table",
  source_row_count: "Counting rows",
  source_column_profile: "Profiling columns",
  source_candidate_key_check: "Checking a candidate key",
  source_relationship_check: "Checking a join",
  source_sample_rows: "Sampling rows",
  source_coverage_check: "Checking coverage",
  flag_untrusted_content: "Flagging untrusted content",
  get_candidate: "Reading the design",
  put_table: "Designing a table",
  put_relationships: "Setting relationships",
  remove_element: "Removing an element",
  map_requirement: "Mapping a requirement",
  record_decision: "Recording a decision",
  update_feasibility: "Assessing feasibility",
  ask_requirements: "Asking the Business Requirements agent",
  answer_architect: "Answering the Architect",
  record_finding: "Recording a finding",
  submit_review: "Submitting the review",
};

/** Saved activity entries are {tool, ok, summary, record} or {kind: "narration", text}; the
 *  live stream's are {name, status, …}. One shape for both. */
function fromSaved(items: unknown[]): ToolActivity[] {
  return (items as Record<string, unknown>[]).map((a) =>
    a.kind === "narration"
      ? { name: "narration", status: "ok", narration: String(a.text ?? "") }
      : { name: String(a.tool ?? a.name ?? ""), status: a.ok === false || a.status === "error" ? "error" : "ok",
          summary: a.summary as string | undefined, record: a.record as ToolActivity["record"], agent: a.agent as ToolActivity["agent"] });
}

const SUBAGENT_LABELS: Record<string, string> = {
  business_requirements: "Requirements",
  validator: "Validator",
  review: "Review",
};

/** Who wrote a reply in an Assistant conversation: the icon and name shown on it. */
const AUTHORS: Record<TurnAgent, { name: string; icon: typeof Bot; tone: string }> = {
  business_requirements: { name: "Requirements agent", icon: ClipboardList, tone: "text-violet-400" },
  architect: { name: "Architect", icon: DraftingCompass, tone: "text-emerald-400" },
  router: { name: "Assistant", icon: Route, tone: "text-muted-foreground" },
};

const ROUTE_TEXT: Record<string, string> = {
  requirements: "Sent to the Requirements agent",
  architect: "Sent to the Architect",
  requirements_then_architect: "Sent to the Requirements agent, then the Architect",
  clarify: "Not sent to an agent yet",
};

type RouteInfo = { route: string; reason: string; source: string };
type LiveStep = { agent?: TurnAgent; text: string; tools: ToolActivity[] };

const AGENTS: Record<ConversationAgent, { title: string; intro: string[]; placeholder: string }> = {
  assistant: {
    title: "AI Assistant",
    intro: [
      "Ask in your own words. Each message goes to the agent that should answer it: the Requirements agent records requirements, definitions and open questions; the Architect designs the model from them and the linked data sources. A request that needs both goes to the Requirements agent first, then the Architect.",
      "Each reply shows which agent wrote it, and each message says where it went and why, with a way to send it to the other agent instead. Nothing an agent records is confirmed, and no model is accepted, until a person does it.",
    ],
    placeholder: "Describe a requirement, answer a question, or ask for a design change…",
  },
  business_requirements: {
    title: "Business Requirements agent",
    intro: [
      "Describe what the business wants to analyze, or attach reports, spreadsheets and glossaries. The agent records requirements, definitions and open questions in the registers.",
      "Nothing it records is confirmed until a person confirms it.",
    ],
    placeholder: "Describe a requirement, answer a question, or attach documents…",
  },
  architect: {
    title: "Architect agent",
    intro: [
      "The Architect designs a Kimball model from the registers and the data sources linked to this model. It checks the data itself, asks the Business Requirements agent about business meanings, and records its decisions.",
      "An independent Validator then reviews the design. An empty model gets the draft applied; otherwise you review the changes on the Review tab before applying them. Accepting the model is always your decision.",
    ],
    placeholder: "Ask for a draft, or ask for changes to the design…",
  },
};

export const DRAFT_PROMPT = "Design a draft model from the requirements and the linked data sources.";

/**
 * Chat with an agent. The Business Requirements agent records requirements, definitions and
 * questions; the Architect designs the model (consulting the Business Requirements agent and
 * reviewed by the Validator). Nothing an agent records is confirmed or accepted until a person
 * does it.
 */
export function AssistantPanel({
  path,
  modelId,
  canEdit,
  onSelectRecord,
  onCandidate,
  draft,
  draftKey = 0,
  startDraft = 0,
  agent = "business_requirements",
  sendTo: sendToRequest,
  sendToKey = 0,
  lockTarget,
  canViewRuns = false,
  onOpenRun,
  onOpenReview,
  className,
}: {
  path: ProjectPath;
  modelId?: string;
  canEdit: boolean;
  onSelectRecord?: (id: string) => void;
  /** Called with the candidate an Architect run produced, once the run ends. */
  onCandidate?: (candidateId: string) => void;
  /** Prefills the composer (e.g. from the wizard's context step). */
  draft?: string;
  /** Changes each time `draft` should be put in the composer again (and focused), even if it's the same text. */
  draftKey?: number;
  /** Architect only: each new value starts a "design a draft model" run, unless one is running. */
  startDraft?: number;
  agent?: ConversationAgent;
  /** Assistant conversations: send the next message to this agent instead of letting the router choose. */
  sendTo?: AgentName;
  /** Changes each time `sendTo` should be applied again. */
  sendToKey?: number;
  /** Assistant conversations: every message goes to this agent (the wizard's requirements step). */
  lockTarget?: AgentName;
  /** Transcripts and monitor assessments need edit access. */
  canViewRuns?: boolean;
  onOpenRun?: (runId: string) => void;
  /** Open the Review tab at a design candidate (Architect replies). */
  onOpenReview?: (candidateId: string) => void;
  className?: string;
}) {
  const conversations = useConversations(path, modelId);
  const createConversation = useCreateConversation(path);
  const conversation = conversations.data?.find((c) => c.agent === agent);
  const info = AGENTS[lockTarget ?? agent];
  const routed = agent === "assistant";
  const messages = useMessages(path, conversation?.id);
  // The monitor reads a run after its reply is saved: wait a few minutes for the newest one.
  const newest = [...(messages.data ?? [])].reverse().find((m) => m.role === "assistant" && m.run_id);
  const waitFor = newest?.run_id && Date.now() - new Date(newest.created_at).getTime() < 5 * 60_000 ? newest.run_id : undefined;
  const assessments = useConversationAssessments(path, conversation?.id, canViewRuns, waitFor ?? undefined);
  const assessmentOf = useMemo(() => new Map((assessments.data ?? []).map((a) => [a.run_id, a])), [assessments.data]);
  // The design each Architect run produced, so its reply can point to Review.
  const candidates = useCandidates(path, modelId ?? "", agent !== "business_requirements" && !!modelId);
  const candidateOf = useMemo(() => new Map((candidates.data ?? []).filter((c) => c.run_id).map((c) => [c.run_id!, c])),
    [candidates.data]);
  const documents = useDocuments(path);
  const invalidate = useInvalidateAfterAgent(path);

  const [text, setText] = useState(draft ?? "");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [running, setRunning] = useState(false);
  const [pending, setPending] = useState<{ text: string; attachments: Attachment[] } | null>(null);
  const [liveText, setLiveText] = useState("");
  const [liveTools, setLiveTools] = useState<ToolActivity[]>([]);
  // A routed turn: where it went, the agents that already answered, and who's answering now.
  const [liveRoute, setLiveRoute] = useState<RouteInfo | null>(null);
  const [liveDone, setLiveDone] = useState<LiveStep[]>([]);
  const [liveStep, setLiveStep] = useState<TurnAgent | undefined>();
  const textNow = useRef("");
  const toolsNow = useRef<ToolActivity[]>([]);
  // "auto" lets the router choose; an agent name sends the next message straight to it.
  const [sendTo, setSendTo] = useState<"auto" | AgentName>("auto");
  // The run being streamed. Mid-run refetches bring in its saved user message, which is
  // already shown as pending, so it's hidden from the history until the run ends.
  const [liveRunId, setLiveRunId] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!draft) return;
    setText(draft);
    if (draftKey) requestAnimationFrame(() => {
      const el = composer.current;
      el?.focus();
      el?.setSelectionRange(draft.length, draft.length);
    });
  }, [draft, draftKey]);
  useEffect(() => {
    if (sendToKey && sendToRequest) setSendTo(sendToRequest);
  }, [sendToRequest, sendToKey]);
  // Start a requested draft once the conversation list is in, so the run reuses the existing
  // conversation instead of creating another. It goes straight to the Architect.
  const startedDraft = useRef(0);
  useEffect(() => {
    if (agent === "business_requirements" || !canEdit || startDraft <= startedDraft.current || !conversations.isSuccess) return;
    startedDraft.current = startDraft;
    void send(DRAFT_PROMPT, { to: "architect" });
  });
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [messages.data?.length, liveText, liveTools.length, pending]);

  const docLabels = useMemo(() => {
    const map = new Map<string, string>();
    for (const d of documents.data ?? []) map.set(d.latest.id, `${d.title} v${d.latest.version_no}`);
    return map;
  }, [documents.data]);

  const attach = async (files: FileList | null) => {
    if (!files?.length) return;
    setUploading(true);
    try {
      for (const file of Array.from(files)) {
        const doc = await uploadDocument(path, file);
        setAttachments((a) => [...a, { versionId: doc.latest.id, label: file.name, status: doc.latest.status }]);
        if (doc.latest.status === "extraction_failed") toast.warning(`${file.name}: text couldn't be extracted (${doc.latest.status_detail})`);
        if (doc.latest.status === "stored_only") toast.info(`${file.name} is stored, but text extraction for images isn't supported yet.`);
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const send = async (override?: string, opts: { to?: AgentName; insteadOf?: string } = {}) => {
    const body = (override ?? text).trim();
    if (running || (!body && attachments.length === 0)) return;
    const sending = { text: body, attachments };
    const target = routed ? (opts.to ?? lockTarget ?? (sendTo === "auto" ? undefined : sendTo)) : undefined;
    setRunning(true);
    setPending(sending);
    setLiveText("");
    setLiveTools([]);
    textNow.current = "";
    toolsNow.current = [];
    setLiveRoute(null);
    setLiveDone([]);
    setLiveStep(undefined);
    if (!opts.to) setSendTo("auto");  // a chosen agent is for one message
    if (override === undefined) setText("");
    setAttachments([]);
    let candidateId: string | undefined;
    try {
      const conv = conversation ?? (await createConversation.mutateAsync({
        model_id: modelId, agent, title: AGENTS[agent].title }));
      const track = (fn: (ts: ToolActivity[]) => ToolActivity[]) =>
        setLiveTools((ts) => (toolsNow.current = fn(ts)));
      await sendAgentMessage(path, conv.id, {
        text: sending.text, attachment_version_ids: sending.attachments.map((a) => a.versionId),
        ...(target ? { agent: target } : {}), ...(target && opts.insteadOf ? { instead_of_run_id: opts.insteadOf } : {}),
      }, (e: AgentEvent) => {
        if (e.step && e.step !== "router") setLiveStep(e.step);
        if (e.type === "route") setLiveRoute({ route: e.route, reason: e.reason, source: e.source });
        else if (e.type === "handoff") {
          // The first agent's reply is saved; keep it on screen while the next agent works.
          const done: LiveStep = { agent: e.from, text: textNow.current, tools: toolsNow.current };
          setLiveDone((d) => [...d, done]);
          setLiveText("");
          setLiveTools([]);
          textNow.current = "";
          toolsNow.current = [];
          setLiveStep(e.to);
          invalidate();
        } else if (e.type === "run") {
          setLiveRunId(e.run_id);
          if (e.redacted_credentials) toast.warning("Credentials were removed from your message before it was saved or sent. Add data sources on the Sources tab instead.");
        }
        else if (e.type === "text") setLiveText((t) => (textNow.current = t + e.text));
        else if (e.type === "tool") {
          track((ts) => {
            const agent = subagent(e);
            if (e.status === "started") return [...ts, { name: e.name, status: "started", agent }];
            const copy = [...ts];
            const i = copy.map((t) => `${t.agent ?? ""}:${t.name}`).lastIndexOf(`${agent ?? ""}:${e.name}`);
            if (i >= 0) copy[i] = { name: e.name, status: e.status, summary: e.summary, record: e.record, agent };
            return copy;
          });
          if (e.status === "ok" && (e.record || e.name === "put_table")) invalidate();  // show changes as they happen
        } else if (e.type === "review") {
          track((ts) => {
            if (e.status === "started") return [...ts, { name: "review", status: "started", agent: "review",
              summary: `The Validator is reviewing the design${e.round ? ` again (round ${e.round})` : ""}` }];
            const copy = [...ts];
            const i = copy.map((t) => t.name).lastIndexOf("review");
            const c = e.counts;
            const summary = e.status === "completed" && c
              ? `Validator round ${e.round}: ${c.passed} passed, ${c.failed} failed, ${c.unverified} unverified`
              : `Validator round ${e.round} didn't complete`;
            if (i >= 0) copy[i] = { name: "review", status: e.status === "completed" ? "ok" : "error", summary, agent: "review" };
            return copy;
          });
        } else if (e.type === "done") {
          candidateId = e.candidate?.id;
          if (e.status !== "completed") {
            toast.warning(e.status === "failed" ? `The agent couldn't finish: ${e.detail}` : `The agent stopped: ${e.detail}`);
          }
        }
      });
    } catch (err) {
      if (override === undefined) setText(sending.text);  // don't lose what the user typed
      setAttachments(sending.attachments);
      toast.error(err instanceof ApiError || err instanceof Error ? err.message : "Sending failed");
    } finally {
      await invalidate();
      setPending(null);
      setLiveText("");
      setLiveTools([]);
      setLiveRoute(null);
      setLiveDone([]);
      setLiveStep(undefined);
      setLiveRunId(null);
      setRunning(false);
      if (candidateId) onCandidate?.(candidateId);
    }
  };

  // Hide the streaming turn's saved messages (its user message and any reply already saved)
  // until it ends: they're on screen as the live turn.
  const history = (messages.data ?? []).filter((m) => !(pending && liveRunId && (m.turn_run_id ?? m.run_id) === liveRunId));
  // A turn's run (transcript and assessment) is shown once, on its last reply.
  const lastOfTurn = new Set<string>();
  {
    const seen = new Set<string>();
    for (const m of [...history].reverse()) {
      const turn = m.turn_run_id ?? m.run_id;
      if (m.role === "assistant" && turn && !seen.has(turn)) { seen.add(turn); lastOfTurn.add(m.id); }
    }
  }
  const canRoute = routed && canEdit && !lockTarget && !running;
  const sendInstead = (to: AgentName, messageText: string, turnRunId: string) =>
    send(messageText, { to, insteadOf: turnRunId });

  return (
    <div className={cn("flex flex-col min-h-0", className)}>
      <div className="flex-1 min-h-0 overflow-y-auto space-y-4 pr-1">
        {history.length === 0 && !pending && (
          <div className="text-sm text-muted-foreground space-y-2 rounded-md border border-dashed p-3">
            <p className="font-medium text-foreground flex items-center gap-2"><Bot className="size-4" /> {info.title}</p>
            {info.intro.map((line) => <p key={line}>{line}</p>)}
            {agent !== "business_requirements" && !lockTarget && canEdit && (
              <Button size="sm" className="mt-1" disabled={running} onClick={() => send(DRAFT_PROMPT, { to: "architect" })}>
                <Sparkles className="size-4" /> Generate draft model
              </Button>
            )}
          </div>
        )}
        {history.map((m) => {
          const turn = m.turn_run_id ?? m.run_id;
          return (
          <Message key={m.id} role={m.role} text={m.text}
            author={routed && m.role === "assistant" ? (m.agent as TurnAgent | null) ?? undefined : undefined}
            route={routed && !lockTarget && m.route ? (
              <RouteLine route={m.route}
                onSendInstead={canRoute && turn ? (to) => sendInstead(to, m.text, turn) : undefined} />
            ) : undefined}
            attachments={m.attachments.map((id) => docLabels.get(id) ?? "attached document")}
            activity={fromSaved(m.activity)} onSelectRecord={onSelectRecord}
            run={m.role === "assistant" && turn && canViewRuns && lastOfTurn.has(m.id)
              ? { assessment: assessmentOf.get(turn), onOpen: onOpenRun ? () => onOpenRun(turn) : undefined }
              : undefined}
            design={m.role === "assistant" && m.run_id && candidateOf.get(m.run_id) && onOpenReview
              ? { status: candidateOf.get(m.run_id)!.status, canApply: canEdit,
                  onOpen: () => onOpenReview(candidateOf.get(m.run_id!)!.id) }
              : undefined} />
          );
        })}
        {pending && (
          <>
            <Message role="user" text={pending.text || "(attached documents)"} attachments={pending.attachments.map((a) => a.label)}
              route={liveRoute && !lockTarget ? <RouteLine route={liveRoute} /> : undefined} />
            {liveDone.map((step, i) => (
              <Message key={i} role="assistant" text={step.text || "(no reply text)"} author={step.agent} activity={step.tools}
                onSelectRecord={onSelectRecord} />
            ))}
            <div className="space-y-2">
              {routed && liveStep && <AuthorLabel agent={liveStep} working />}
              <LiveActivity tools={liveTools} onSelectRecord={onSelectRecord} />
              {liveText ? (
                <>
                  <Message role="assistant" text={liveText} />
                  {/* Work continues after the reply streams (e.g. the Validator's review), and
                      the activity list above is scrolled away by then: pin the latest step here. */}
                  <CurrentStep tool={liveTools[liveTools.length - 1]} />
                </>
              ) : (
                <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> Thinking…</div>
              )}
            </div>
          </>
        )}
        <div ref={bottom} />
      </div>

      {canEdit ? (
        <div className="pt-3 space-y-2">
          {attachments.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {attachments.map((a) => (
                <Badge key={a.versionId} variant="secondary" className="gap-1">
                  <FileText className="size-3" /> {a.label}
                  <button type="button" aria-label={`Remove ${a.label}`} onClick={() => setAttachments((x) => x.filter((y) => y.versionId !== a.versionId))}>
                    <X className="size-3" />
                  </button>
                </Badge>
              ))}
            </div>
          )}
          <Textarea
            ref={composer}
            rows={3}
            value={text}
            disabled={running}
            placeholder={routed && !lockTarget && sendTo !== "auto" ? `Message for the ${AUTHORS[sendTo].name}…` : info.placeholder}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                send();
              }
            }}
          />
          <div className="flex items-center gap-2">
            <input ref={fileInput} type="file" multiple hidden accept=".pdf,.docx,.xlsx,.csv,.txt,.md,.png,.jpg,.jpeg"
              onChange={(e) => attach(e.target.files)} />
            <Button size="sm" variant="outline" disabled={running || uploading} onClick={() => fileInput.current?.click()}>
              {uploading ? <Loader2 className="size-4 animate-spin" /> : <Paperclip className="size-4" />} Attach
            </Button>
            {routed && !lockTarget ? (
              <div className="flex items-center gap-1 text-xs text-muted-foreground" role="radiogroup" aria-label="Send to">
                <span>To</span>
                <div className="flex rounded-md border p-0.5">
                  {([["auto", "Auto"], ["business_requirements", "Requirements"], ["architect", "Architect"]] as const).map(([value, label]) => (
                    <button key={value} type="button" role="radio" aria-checked={sendTo === value} disabled={running}
                      title={value === "auto" ? "The assistant picks the agent" : `Send the next message to the ${AUTHORS[value].name}`}
                      className={cn("px-1.5 py-0.5 rounded", sendTo === value ? "bg-accent text-foreground" : "hover:text-foreground")}
                      onClick={() => setSendTo(value)}>{label}</button>
                  ))}
                </div>
              </div>
            ) : (
              <span className="text-xs text-muted-foreground">Enter to send · Shift+Enter for a new line</span>
            )}
            <Button size="sm" className="ml-auto" disabled={running || uploading || (!text.trim() && attachments.length === 0)} onClick={() => send()}>
              {running ? <Loader2 className="size-4 animate-spin" /> : <Send className="size-4" />} Send
            </Button>
          </div>
        </div>
      ) : (
        <p className="pt-3 text-xs text-muted-foreground">You have read-only access, so you can read this conversation but not message the agent.</p>
      )}
    </div>
  );
}

function Message({
  role,
  text,
  author,
  route,
  attachments = [],
  activity = [],
  onSelectRecord,
  run,
  design,
}: {
  role: string;
  text: string;
  /** Assistant conversations: the agent that wrote the reply. */
  author?: TurnAgent;
  /** Assistant conversations: the route line under a user message. */
  route?: React.ReactNode;
  attachments?: string[];
  activity?: ToolActivity[];
  onSelectRecord?: (id: string) => void;
  /** For a saved reply: its run's monitor assessment and a way to open the run. */
  run?: { assessment: Assessment | undefined; onOpen?: () => void };
  /** For an Architect reply: the design it produced and a way to open it on Review. */
  design?: { status: string; canApply: boolean; onOpen: () => void };
}) {
  const isUser = role === "user";
  const Icon = author ? AUTHORS[author].icon : Bot;
  return (
    <div className={cn("flex gap-2", isUser && "flex-row-reverse")}>
      <div className={cn("mt-0.5 shrink-0 text-muted-foreground", author && AUTHORS[author].tone)}>
        {isUser ? <User className="size-4" /> : <Icon className="size-4" aria-label={author ? AUTHORS[author].name : undefined} />}
      </div>
      <div className={cn("min-w-0 space-y-1.5", isUser && "items-end flex flex-col")}>
        {author && <p className={cn("text-xs font-medium", AUTHORS[author].tone)}>{AUTHORS[author].name}</p>}
        {activity.length > 0 && <ActivityList tools={activity} onSelectRecord={onSelectRecord} />}
        {isUser ? (
          <div className="rounded-lg px-3 py-2 text-sm whitespace-pre-wrap break-words bg-primary/15">{text}</div>
        ) : (
          // Agent replies are Markdown. react-markdown renders no raw HTML, so model output
          // can't inject markup into the page.
          <div className="rounded-lg px-3 py-2 text-sm break-words bg-muted space-y-2 [&_ul]:list-disc [&_ol]:list-decimal [&_ul]:pl-5 [&_ol]:pl-5 [&_li]:my-0.5 [&_strong]:font-semibold [&_code]:font-mono [&_code]:text-xs [&_h1]:font-semibold [&_h2]:font-semibold [&_h3]:font-semibold">
            <Markdown>{text}</Markdown>
          </div>
        )}
        {attachments.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {attachments.map((a, i) => (
              <Badge key={i} variant="outline" className="gap-1 font-normal"><FileText className="size-3" /> {a}</Badge>
            ))}
          </div>
        )}
        {route}
        {design?.status === "proposed" && (
          <div className="flex items-center gap-2 rounded-md border border-primary/40 bg-primary/5 px-2.5 py-1.5 text-xs">
            <span className="flex-1 min-w-0">These changes aren't in the model yet.</span>
            <Button size="sm" className="h-7 shrink-0" onClick={design.onOpen}>{design.canApply ? "Review and apply" : "Open Review"}</Button>
          </div>
        )}
        {run && (
          <>
            <FlaggedBanner assessment={run.assessment} onOpen={run.onOpen} />
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              {run.assessment && <AssessmentChip assessment={run.assessment} onClick={run.onOpen} />}
              {run.onOpen && (
                <button type="button" className="inline-flex items-center gap-1 hover:text-foreground" onClick={run.onOpen}>
                  <ScrollText className="size-3" /> View run
                </button>
              )}
              {design && design.status !== "proposed" && (
                <button type="button" className="inline-flex items-center gap-1 hover:text-foreground" onClick={design.onOpen}>
                  <ShieldCheck className="size-3" /> Design on Review ({design.status})
                </button>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function ActivityList({ tools, onSelectRecord }: { tools: ToolActivity[]; onSelectRecord?: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const records = tools.filter((t) => t.record?.code);
  const notes = tools.filter((t) => t.narration).length;
  const actions = tools.length - notes;
  return (
    <div className="text-xs text-muted-foreground">
      <button type="button" className="flex items-center gap-1 hover:text-foreground" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
        {actions} action{actions === 1 ? "" : "s"}
        {records.length > 0 && ` · ${records.length} register change${records.length === 1 ? "" : "s"}`}
        {notes > 0 && ` · ${notes} progress note${notes === 1 ? "" : "s"}`}
      </button>
      {!open && records.length > 0 && (
        <div className="mt-1 space-y-1">
          {groupByAction(records).map((g) => (
            <div key={g.label} className="flex flex-wrap items-center gap-1">
              <span className="mr-0.5">{g.label}</span>
              {g.items.map((r) => (
                <RecordChip key={r.id} code={r.code} onClick={() => onSelectRecord?.(r.id)} />
              ))}
            </div>
          ))}
        </div>
      )}
      {open && (
        <ul className="mt-1 space-y-0.5">
          {tools.map((t, i) => t.narration ? (
            <li key={i} className="flex items-start gap-1.5 py-0.5 text-foreground/80">
              <MessageSquareText className="size-3 shrink-0 mt-0.5" />
              <span className="whitespace-pre-wrap break-words">{t.narration}</span>
            </li>
          ) : (
            <li key={i} className={cn("flex items-center gap-1.5", t.status === "error" && "text-amber-500")}>
              {t.status === "error" ? <XCircle className="size-3 shrink-0" /> : <span className="size-1.5 rounded-full bg-current shrink-0" />}
              {t.agent && t.agent !== "review" && <SubagentTag agent={t.agent} />}
              <span className="truncate">{t.summary ?? TOOL_LABELS[t.name] ?? t.name}</span>
              {t.record?.code && <RecordChip code={t.record.code} onClick={() => onSelectRecord?.(t.record!.id)} />}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const ACTION_LABELS: [string, string][] = [
  ["answered", "Answered"], ["deferred", "Deferred"], ["resolved", "Resolved"],
  ["added", "Added"], ["updated", "Updated"],
];

/** "Answered Q-001 Q-008 · Added DEF-020": each record once per action, answers first. */
function groupByAction(tools: ToolActivity[]) {
  const groups = new Map<string, Map<string, { id: string; code: string }>>();
  for (const t of tools) {
    const action = t.record!.action ?? "changed";
    const g = groups.get(action) ?? new Map();
    g.set(t.record!.id, { id: t.record!.id, code: t.record!.code });
    groups.set(action, g);
  }
  const order = [...ACTION_LABELS, ["changed", "Changed"] as [string, string]];
  return order
    .filter(([key]) => groups.has(key))
    .map(([key, label]) => ({ label, items: [...groups.get(key)!.values()] }));
}

function LiveActivity({ tools, onSelectRecord }: { tools: ToolActivity[]; onSelectRecord?: (id: string) => void }) {
  if (tools.length === 0) return null;
  return (
    <ul className="text-xs text-muted-foreground space-y-0.5 pl-6">
      {tools.map((t, i) => (
        <li key={i} className={cn("flex items-center gap-1.5", t.status === "error" && "text-amber-500")}>
          {t.status === "started" ? <Loader2 className="size-3 animate-spin shrink-0" />
            : t.status === "error" ? <XCircle className="size-3 shrink-0" />
            : t.agent === "review" ? <ShieldCheck className="size-3 shrink-0" /> : <span className="size-1.5 rounded-full bg-current shrink-0" />}
          {t.agent && t.agent !== "review" && <SubagentTag agent={t.agent} />}
          <span className="truncate">{t.status === "started" ? (t.summary ?? `${TOOL_LABELS[t.name] ?? t.name}…`) : t.summary}</span>
          {t.record?.code && <RecordChip code={t.record.code} onClick={() => onSelectRecord?.(t.record!.id)} />}
        </li>
      ))}
    </ul>
  );
}

function RecordChip({ code, onClick }: { code: string; onClick?: () => void }) {
  return (
    <button type="button" onClick={onClick} className="shrink-0 whitespace-nowrap font-mono text-[11px] rounded border px-1 hover:bg-accent text-foreground">
      {code}
    </button>
  );
}

/** Marks activity that a consulted or reviewing agent did inside the Architect's run. */
function SubagentTag({ agent }: { agent: string }) {
  return (
    <span className={cn("shrink-0 rounded px-1 text-[10px] font-medium uppercase tracking-wide",
      agent === "validator" ? "bg-sky-500/15 text-sky-400" : "bg-violet-500/15 text-violet-400")}>
      {SUBAGENT_LABELS[agent] ?? agent}
    </span>
  );
}

function CurrentStep({ tool }: { tool: ToolActivity | undefined }) {
  return (
    <div className="flex items-center gap-1.5 pl-6 text-xs text-muted-foreground min-w-0">
      <Loader2 className="size-3 animate-spin shrink-0" />
      {tool?.agent && tool.agent !== "review" && <SubagentTag agent={tool.agent} />}
      <span className="truncate">
        {tool ? (tool.status === "started" ? (tool.summary ?? `${TOOL_LABELS[tool.name] ?? tool.name}…`) : tool.summary) : "Working…"}
      </span>
    </div>
  );
}

/** Who is answering a live routed turn. */
function AuthorLabel({ agent, working }: { agent: TurnAgent; working?: boolean }) {
  const a = AUTHORS[agent];
  return (
    <p className={cn("flex items-center gap-1.5 text-xs font-medium", a.tone)}>
      <a.icon className="size-4" /> {a.name}{working && <span className="font-normal text-muted-foreground">is working…</span>}
    </p>
  );
}

/** Where a message went and why, with "send to the other agent instead". */
function RouteLine({ route, onSendInstead }: { route: RouteInfo; onSendInstead?: (to: AgentName) => void }) {
  const others: AgentName[] = route.route === "requirements" ? ["architect"]
    : route.route === "architect" ? ["business_requirements"]
    : route.route === "clarify" ? ["business_requirements", "architect"] : [];
  return (
    <div className="flex flex-wrap items-center justify-end gap-x-2 gap-y-1 text-xs text-muted-foreground max-w-full">
      <span className="inline-flex items-center gap-1 min-w-0" title={route.reason}>
        <Route className="size-3 shrink-0" />
        <span className="font-medium">{ROUTE_TEXT[route.route] ?? route.route}</span>
        <span className="truncate">· {route.reason}</span>
      </span>
      {onSendInstead && others.map((to) => (
        <button key={to} type="button" className="underline-offset-2 hover:underline hover:text-foreground" onClick={() => onSendInstead(to)}>
          {route.route === "clarify" ? `Send to the ${AUTHORS[to].name}` : `Send to the ${AUTHORS[to].name} instead`}
        </button>
      ))}
    </div>
  );
}
