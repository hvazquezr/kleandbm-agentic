import { Bot, ChevronDown, ChevronRight, FileText, Loader2, Paperclip, Send, User, X, XCircle } from "lucide-react";
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
  type ProjectPath,
} from "@/api/hooks";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

type Attachment = { versionId: string; label: string; status: string };
type ToolActivity = { name: string; status: "started" | "ok" | "error"; summary?: string; record?: { id: string; code: string } | null };

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
};

/**
 * Chat with the Business Requirements agent. The agent records requirements, definitions and
 * questions in the registers as it works; nothing it records is confirmed until a person
 * confirms it in the Registers tab.
 */
export function AssistantPanel({
  path,
  modelId,
  canEdit,
  onSelectRecord,
  draft,
  className,
}: {
  path: ProjectPath;
  modelId?: string;
  canEdit: boolean;
  onSelectRecord?: (id: string) => void;
  /** Prefills the composer (e.g. from the wizard's context step). */
  draft?: string;
  className?: string;
}) {
  const conversations = useConversations(path, modelId);
  const createConversation = useCreateConversation(path);
  const conversation = conversations.data?.[0];
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

  const send = async () => {
    if (running || (!text.trim() && attachments.length === 0)) return;
    const sending = { text: text.trim(), attachments };
    setRunning(true);
    setPending(sending);
    setLiveText("");
    setLiveTools([]);
    setText("");
    setAttachments([]);
    try {
      const conv = conversation ?? (await createConversation.mutateAsync({ model_id: modelId, title: "Requirements" }));
      await sendAgentMessage(path, conv.id, { text: sending.text, attachment_version_ids: sending.attachments.map((a) => a.versionId) }, (e: AgentEvent) => {
        if (e.type === "run") setLiveRunId(e.run_id);
        else if (e.type === "text") setLiveText((t) => t + e.text);
        else if (e.type === "tool") {
          setLiveTools((ts) => {
            if (e.status === "started") return [...ts, { name: e.name, status: "started" }];
            const copy = [...ts];
            const i = copy.map((t) => t.name).lastIndexOf(e.name);
            if (i >= 0) copy[i] = { name: e.name, status: e.status, summary: e.summary, record: e.record };
            return copy;
          });
          if (e.status === "ok" && e.record) invalidate();  // show register changes as they happen
        } else if (e.type === "done" && e.status !== "completed") {
          toast.warning(e.status === "failed" ? `The agent couldn't finish: ${e.detail}` : `The agent stopped: ${e.detail}`);
        }
      });
    } catch (err) {
      setText(sending.text);  // don't lose what the user typed
      setAttachments(sending.attachments);
      toast.error(err instanceof ApiError || err instanceof Error ? err.message : "Sending failed");
    } finally {
      await invalidate();
      setPending(null);
      setLiveText("");
      setLiveTools([]);
      setLiveRunId(null);
      setRunning(false);
    }
  };

  const history = (messages.data ?? []).filter((m) => !(pending && liveRunId && m.run_id === liveRunId));

  return (
    <div className={cn("flex flex-col min-h-0", className)}>
      <div className="flex-1 min-h-0 overflow-y-auto space-y-4 pr-1">
        {history.length === 0 && !pending && (
          <div className="text-sm text-muted-foreground space-y-2 rounded-md border border-dashed p-3">
            <p className="font-medium text-foreground flex items-center gap-2"><Bot className="size-4" /> Business Requirements agent</p>
            <p>Describe what the business wants to analyze, or attach reports, spreadsheets and glossaries. The agent records requirements, definitions and open questions in the registers.</p>
            <p>Nothing it records is confirmed until a person confirms it.</p>
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
              {liveText ? <Message role="assistant" text={liveText} /> : (
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
            placeholder="Describe a requirement, answer a question, or attach documents…"
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
            <Button size="sm" className="ml-auto" disabled={running || uploading || (!text.trim() && attachments.length === 0)} onClick={send}>
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
            : t.status === "error" ? <XCircle className="size-3 shrink-0" /> : <span className="size-1.5 rounded-full bg-current shrink-0" />}
          <span className="truncate">{t.status === "started" ? `${TOOL_LABELS[t.name] ?? t.name}…` : t.summary}</span>
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
