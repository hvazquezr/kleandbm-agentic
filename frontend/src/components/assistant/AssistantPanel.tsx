import { Bot, ChevronDown, ChevronRight, FileText, Loader2, Paperclip, Send, ShieldCheck, Sparkles, User, X, XCircle } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import Markdown from "react-markdown";
import { toast } from "sonner";
import { sendAgentMessage, uploadDocument, type AgentEvent } from "@/api/agent";
import { ApiError } from "@/api/client";
import {
  useConversations,
  useCreateConversation,
  useDocuments,
  useInvalidateAfterAgent,
  useMessages,
  type AgentName,
  type ProjectPath,
} from "@/api/hooks";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

type Attachment = { versionId: string; label: string; status: string };
type ToolActivity = {
  name: string;
  status: "started" | "ok" | "error";
  summary?: string;
  record?: { id: string; code: string } | null;
  /** The subgraph that made the call, when the Architect consulted or was reviewed. */
  agent?: "business_requirements" | "validator" | "review";
};

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

const SUBAGENT_LABELS: Record<string, string> = {
  business_requirements: "Requirements",
  validator: "Validator",
  review: "Review",
};

const AGENTS: Record<AgentName, { title: string; intro: string[]; placeholder: string }> = {
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
  agent = "business_requirements",
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
  agent?: AgentName;
  className?: string;
}) {
  const conversations = useConversations(path, modelId);
  const createConversation = useCreateConversation(path);
  const conversation = conversations.data?.find((c) => c.agent === agent);
  const info = AGENTS[agent];
  const messages = useMessages(path, conversation?.id);
  const documents = useDocuments(path);
  const invalidate = useInvalidateAfterAgent(path);

  const [text, setText] = useState(draft ?? "");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [running, setRunning] = useState(false);
  const [pending, setPending] = useState<{ text: string; attachments: Attachment[] } | null>(null);
  const [liveText, setLiveText] = useState("");
  const [liveTools, setLiveTools] = useState<ToolActivity[]>([]);
  // The run being streamed. Mid-run refetches bring in its saved user message, which is
  // already shown as pending, so it's hidden from the history until the run ends.
  const [liveRunId, setLiveRunId] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (draft) setText(draft);
  }, [draft]);
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

  const send = async (override?: string) => {
    const body = (override ?? text).trim();
    if (running || (!body && attachments.length === 0)) return;
    const sending = { text: body, attachments };
    setRunning(true);
    setPending(sending);
    setLiveText("");
    setLiveTools([]);
    if (override === undefined) setText("");
    setAttachments([]);
    let candidateId: string | undefined;
    try {
      const conv = conversation ?? (await createConversation.mutateAsync({
        model_id: modelId, agent, title: agent === "architect" ? "Architect" : "Requirements" }));
      await sendAgentMessage(path, conv.id, { text: sending.text, attachment_version_ids: sending.attachments.map((a) => a.versionId) }, (e: AgentEvent) => {
        if (e.type === "run") {
          setLiveRunId(e.run_id);
          if (e.redacted_credentials) toast.warning("Credentials were removed from your message before it was saved or sent. Add data sources on the Sources tab instead.");
        }
        else if (e.type === "text") setLiveText((t) => t + e.text);
        else if (e.type === "tool") {
          setLiveTools((ts) => {
            if (e.status === "started") return [...ts, { name: e.name, status: "started", agent: e.agent }];
            const copy = [...ts];
            const i = copy.map((t) => `${t.agent ?? ""}:${t.name}`).lastIndexOf(`${e.agent ?? ""}:${e.name}`);
            if (i >= 0) copy[i] = { name: e.name, status: e.status, summary: e.summary, record: e.record, agent: e.agent };
            return copy;
          });
          if (e.status === "ok" && (e.record || e.name === "put_table")) invalidate();  // show changes as they happen
        } else if (e.type === "review") {
          setLiveTools((ts) => {
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
      setLiveRunId(null);
      setRunning(false);
      if (candidateId) onCandidate?.(candidateId);
    }
  };

  const history = (messages.data ?? []).filter((m) => !(pending && liveRunId && m.run_id === liveRunId));

  return (
    <div className={cn("flex flex-col min-h-0", className)}>
      <div className="flex-1 min-h-0 overflow-y-auto space-y-4 pr-1">
        {history.length === 0 && !pending && (
          <div className="text-sm text-muted-foreground space-y-2 rounded-md border border-dashed p-3">
            <p className="font-medium text-foreground flex items-center gap-2"><Bot className="size-4" /> {info.title}</p>
            {info.intro.map((line) => <p key={line}>{line}</p>)}
            {agent === "architect" && canEdit && (
              <Button size="sm" className="mt-1" disabled={running} onClick={() => send(DRAFT_PROMPT)}>
                <Sparkles className="size-4" /> Generate draft model
              </Button>
            )}
          </div>
        )}
        {history.map((m) => (
          <Message key={m.id} role={m.role} text={m.text}
            attachments={m.attachments.map((id) => docLabels.get(id) ?? "attached document")}
            activity={m.activity as ToolActivity[]} onSelectRecord={onSelectRecord} />
        ))}
        {pending && (
          <>
            <Message role="user" text={pending.text || "(attached documents)"} attachments={pending.attachments.map((a) => a.label)} />
            <div className="space-y-2">
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
            rows={3}
            value={text}
            disabled={running}
            placeholder={info.placeholder}
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
            <span className="text-xs text-muted-foreground">Enter to send · Shift+Enter for a new line</span>
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
  attachments = [],
  activity = [],
  onSelectRecord,
}: {
  role: string;
  text: string;
  attachments?: string[];
  activity?: ToolActivity[];
  onSelectRecord?: (id: string) => void;
}) {
  const isUser = role === "user";
  return (
    <div className={cn("flex gap-2", isUser && "flex-row-reverse")}>
      <div className="mt-0.5 shrink-0 text-muted-foreground">{isUser ? <User className="size-4" /> : <Bot className="size-4" />}</div>
      <div className={cn("min-w-0 space-y-1.5", isUser && "items-end flex flex-col")}>
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
      </div>
    </div>
  );
}

function ActivityList({ tools, onSelectRecord }: { tools: ToolActivity[]; onSelectRecord?: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const records = tools.filter((t) => t.record?.code);
  return (
    <div className="text-xs text-muted-foreground">
      <button type="button" className="flex items-center gap-1 hover:text-foreground" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
        {tools.length} action{tools.length === 1 ? "" : "s"}
        {records.length > 0 && ` · ${records.length} register change${records.length === 1 ? "" : "s"}`}
      </button>
      {!open && records.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-1">
          {records.map((t, i) => (
            <RecordChip key={i} code={t.record!.code} onClick={() => onSelectRecord?.(t.record!.id)} />
          ))}
        </div>
      )}
      {open && (
        <ul className="mt-1 space-y-0.5">
          {tools.map((t, i) => (
            <li key={i} className={cn("flex items-center gap-1.5", t.status === "error" && "text-amber-500")}>
              {t.status === "error" ? <XCircle className="size-3 shrink-0" /> : <span className="size-1.5 rounded-full bg-current shrink-0" />}
              <span className="truncate">{t.summary ?? TOOL_LABELS[t.name] ?? t.name}</span>
              {t.record?.code && <RecordChip code={t.record.code} onClick={() => onSelectRecord?.(t.record!.id)} />}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
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
