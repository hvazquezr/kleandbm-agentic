import { ArrowLeft, ArrowRight, Check, Database, FileText, Loader2, MessageSquare, Sparkles, Upload } from "lucide-react";
import { useRef, useState } from "react";
import { toast } from "sonner";
import { Link, useLocation, useParams } from "wouter";
import { uploadDocument } from "@/api/agent";
import {
  useCallSourceTool,
  useCreateModel,
  useCreateSource,
  useDocuments,
  useProject,
  useSnapshot,
  useSources,
  type ProjectPath,
} from "@/api/hooks";
import { AssistantPanel } from "@/components/assistant/AssistantPanel";
import { Field } from "@/components/editor/fields";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { humanize } from "@/lib/labels";
import { cn } from "@/lib/utils";

const STEPS = [
  { key: "context", label: "Context", icon: Sparkles },
  { key: "documents", label: "Documents", icon: FileText },
  { key: "source", label: "Source (optional)", icon: Database },
  { key: "requirements", label: "Requirements", icon: MessageSquare },
  { key: "finish", label: "Finish", icon: Check },
] as const;

type Context = { name: string; domain: string; goal: string; notes: string };

export default function NewModelWizard() {
  const { tenantId, projectId } = useParams<{ tenantId: string; projectId: string }>();
  const path: ProjectPath = { tenant_id: tenantId, project_id: projectId };
  const project = useProject(path);
  const [step, setStep] = useState(0);
  const [modelId, setModelId] = useState<string | null>(null);
  const [ctx, setCtx] = useState<Context>({ name: "", domain: "", goal: "", notes: "" });

  const isOwner = project.data?.role === "owner";
  const canEdit = project.data && project.data.role !== "viewer";
  if (project.data && !canEdit) {
    return <p className="text-muted-foreground">You need edit access to create a model in this project.</p>;
  }

  return (
    <div className="space-y-6 max-w-4xl mx-auto">
      <Link href={`/t/${tenantId}/p/${projectId}`} className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> {project.data?.name ?? "Project"}
      </Link>
      <div>
        <h1 className="text-2xl font-semibold">New data model</h1>
        <p className="text-sm text-muted-foreground">
          Share what you have. Partial information is fine: the agent records what's supported and asks about the rest.
        </p>
      </div>

      <ol className="flex flex-wrap gap-2">
        {STEPS.map((s, i) => (
          <li key={s.key} className={cn(
            "flex items-center gap-1.5 rounded-full border px-3 py-1 text-sm",
            i === step ? "border-primary text-foreground" : i < step ? "text-foreground" : "text-muted-foreground",
          )}>
            {i < step ? <Check className="size-3.5 text-green-500" /> : <s.icon className="size-3.5" />} {s.label}
          </li>
        ))}
      </ol>

      {step === 0 && (
        <ContextStep path={path} ctx={ctx} setCtx={setCtx} modelId={modelId}
          onDone={(id) => { setModelId(id); setStep(1); }} />
      )}
      {step === 1 && <DocumentsStep path={path} onBack={() => setStep(0)} onNext={() => setStep(2)} />}
      {step === 2 && <SourceStep path={path} isOwner={!!isOwner} onBack={() => setStep(1)} onNext={() => setStep(3)} />}
      {step === 3 && modelId && (
        <RequirementsStep path={path} modelId={modelId} draft={kickoff(ctx)} onBack={() => setStep(2)} onNext={() => setStep(4)} />
      )}
      {step === 4 && modelId && <FinishStep path={path} modelId={modelId} onBack={() => setStep(3)} />}
    </div>
  );
}

function kickoff(ctx: Context): string {
  const parts = [`We're building an analytical data model${ctx.domain ? ` for ${ctx.domain}` : ""}.`];
  if (ctx.goal) parts.push(`Goal: ${ctx.goal}`);
  if (ctx.notes) parts.push(ctx.notes);
  parts.push("Please review the documents we uploaded and record the requirements, business definitions and open questions you find. Partial information is fine; tell me what's missing or conflicting.");
  return parts.join("\n\n");
}

function Footer({ onBack, children }: { onBack?: () => void; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2 pt-2">
      {onBack && <Button variant="ghost" onClick={onBack}><ArrowLeft className="size-4" /> Back</Button>}
      <div className="ml-auto flex gap-2">{children}</div>
    </div>
  );
}

function ContextStep({ path, ctx, setCtx, modelId, onDone }: {
  path: ProjectPath; ctx: Context; setCtx: (c: Context) => void; modelId: string | null; onDone: (id: string) => void;
}) {
  const createModel = useCreateModel(path);
  const next = async () => {
    if (modelId) return onDone(modelId);  // already created; don't create a duplicate on Back/Next
    const description = [ctx.domain && `Domain: ${ctx.domain}`, ctx.goal && `Goal: ${ctx.goal}`].filter(Boolean).join(" · ") || undefined;
    try {
      const model = await createModel.mutateAsync({ name: ctx.name.trim(), description });
      onDone(model.id);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not create the model");
    }
  };
  return (
    <Card><CardContent className="space-y-4 pt-6">
      <Field label="Model name"><Input autoFocus value={ctx.name} onChange={(e) => setCtx({ ...ctx, name: e.target.value })} placeholder="e.g. Retail sales analytics" /></Field>
      <Field label="Business domain (optional)"><Input value={ctx.domain} onChange={(e) => setCtx({ ...ctx, domain: e.target.value })} placeholder="e.g. Northstar Retail, sales and returns" /></Field>
      <Field label="What should the model help analyze? (optional)">
        <Textarea rows={2} value={ctx.goal} onChange={(e) => setCtx({ ...ctx, goal: e.target.value })} placeholder="e.g. Monthly revenue, returns and profitability by channel, store and category" />
      </Field>
      <Field label="Anything else the agent should know? (optional)">
        <Textarea rows={3} value={ctx.notes} onChange={(e) => setCtx({ ...ctx, notes: e.target.value })} />
      </Field>
      <Footer>
        <Button onClick={next} disabled={!ctx.name.trim() || createModel.isPending}>
          {createModel.isPending && <Loader2 className="size-4 animate-spin" />} {modelId ? "Next" : "Create model"} <ArrowRight className="size-4" />
        </Button>
      </Footer>
    </CardContent></Card>
  );
}

function DocumentsStep({ path, onBack, onNext }: { path: ProjectPath; onBack: () => void; onNext: () => void }) {
  const documents = useDocuments(path);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const upload = async (files: FileList | null) => {
    if (!files?.length) return;
    setBusy(true);
    for (const file of Array.from(files)) {
      try {
        await uploadDocument(path, file);
      } catch (err) {
        toast.error(`${file.name}: ${err instanceof Error ? err.message : "upload failed"}`);
      }
    }
    await documents.refetch();
    setBusy(false);
    if (input.current) input.current.value = "";
  };
  return (
    <Card><CardContent className="space-y-4 pt-6">
      <p className="text-sm text-muted-foreground">
        Reports, dashboards, spreadsheets, glossaries or source documentation. Documents belong to the project, so every model in it can use them.
      </p>
      <div
        className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground"
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => { e.preventDefault(); upload(e.dataTransfer.files); }}
      >
        <Upload className="size-5 mx-auto mb-2" />
        Drop files here, or{" "}
        <button type="button" className="underline" onClick={() => input.current?.click()}>choose files</button>
        <div className="text-xs mt-1">PDF, Word, Excel, CSV, text, Markdown. Images are stored but not read yet.</div>
        <input ref={input} type="file" multiple hidden accept=".pdf,.docx,.xlsx,.csv,.txt,.md,.png,.jpg,.jpeg" onChange={(e) => upload(e.target.files)} />
      </div>
      {busy && <p className="text-sm flex items-center gap-2"><Loader2 className="size-4 animate-spin" /> Uploading and extracting…</p>}
      <ul className="space-y-1.5">
        {documents.data?.map((d) => (
          <li key={d.id} className="flex items-center gap-2 text-sm rounded-md border px-3 py-2">
            <FileText className="size-4 text-muted-foreground" />
            <span className="truncate">{d.title}</span>
            <span className="text-xs text-muted-foreground">v{d.latest.version_no}</span>
            <Badge variant="outline" className={cn("ml-auto font-normal",
              d.latest.status === "ready" ? "text-green-400" : d.latest.status.includes("failed") ? "text-red-400" : "text-amber-400")}>
              {humanize(d.latest.status)}
            </Badge>
          </li>
        ))}
      </ul>
      <Footer onBack={onBack}>
        <Button onClick={onNext}>{documents.data?.length ? "Next" : "Continue without documents"} <ArrowRight className="size-4" /></Button>
      </Footer>
    </CardContent></Card>
  );
}

function SourceStep({ path, isOwner, onBack, onNext }: { path: ProjectPath; isOwner: boolean; onBack: () => void; onNext: () => void }) {
  const sources = useSources(path);
  const createSource = useCreateSource(path);
  const callTool = useCallSourceTool(path);
  const [form, setForm] = useState({ name: "", host: "127.0.0.1", port: "3306", database: "", username: "", password: "" });
  const [result, setResult] = useState<string | null>(null);

  const connect = async () => {
    setResult(null);
    try {
      const conn = await createSource.mutateAsync({ ...form, port: Number(form.port) || 3306 });
      const evidence = await callTool.mutateAsync({ connectionId: conn.id, tool: "list_tables", purpose: "Connection check from the new-model wizard" });
      if (evidence.is_error) {
        setResult(`Saved, but the check failed: ${evidence.error}`);
      } else {
        const tables = (evidence.result?.tables as unknown[] | undefined)?.length ?? 0;
        setResult(`Connected: ${tables} table${tables === 1 ? "" : "s"} found.`);
        setForm({ ...form, password: "" });
      }
    } catch (err) {
      setResult(err instanceof Error ? err.message : "Could not connect");
    }
  };

  return (
    <Card><CardContent className="space-y-4 pt-6">
      <p className="text-sm text-muted-foreground">
        Optional. A read-only connection to a MySQL source lets the agents check what the data can support. You can skip this and add it later; the model can start from documents and conversation alone.
      </p>
      {sources.data && sources.data.length > 0 && (
        <ul className="space-y-1.5">
          {sources.data.map((s) => (
            <li key={s.id} className="flex items-center gap-2 text-sm rounded-md border px-3 py-2">
              <Database className="size-4 text-muted-foreground" /> {s.name}
              <span className="text-xs text-muted-foreground">{s.username}@{s.host}:{s.port}/{s.database}</span>
              {s.last_verified_at && <Badge variant="outline" className="ml-auto font-normal text-green-400">verified</Badge>}
            </li>
          ))}
        </ul>
      )}
      {isOwner ? (
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name"><Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="e.g. Northstar operations" /></Field>
          <Field label="Database"><Input value={form.database} onChange={(e) => setForm({ ...form, database: e.target.value })} /></Field>
          <Field label="Host"><Input value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} /></Field>
          <Field label="Port"><Input value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} /></Field>
          <Field label="Read-only user"><Input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} /></Field>
          <Field label="Password"><Input type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></Field>
          <div className="col-span-2 flex items-center gap-3">
            <Button variant="outline" onClick={connect}
              disabled={!form.name || !form.database || !form.username || createSource.isPending || callTool.isPending}>
              {(createSource.isPending || callTool.isPending) && <Loader2 className="size-4 animate-spin" />} Save and test
            </Button>
            {result && <span className="text-sm">{result}</span>}
          </div>
          <p className="col-span-2 text-xs text-muted-foreground">Use an account with SELECT-only grants. The password is encrypted and never shown again.</p>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">Only project owners can connect sources.</p>
      )}
      <Footer onBack={onBack}>
        <Button variant={sources.data?.length ? "default" : "outline"} onClick={onNext}>
          {sources.data?.length ? "Next" : "Skip"} <ArrowRight className="size-4" />
        </Button>
      </Footer>
    </CardContent></Card>
  );
}

function RequirementsStep({ path, modelId, draft, onBack, onNext }: {
  path: ProjectPath; modelId: string; draft: string; onBack: () => void; onNext: () => void;
}) {
  return (
    <Card><CardContent className="space-y-3 pt-6">
      <p className="text-sm text-muted-foreground">
        Chat with the Business Requirements agent. We've drafted an opening message from your context; edit it and send when ready.
        Answer the agent's questions as far as you can. Anything still open stays recorded as a question.
      </p>
      <AssistantPanel path={path} modelId={modelId} canEdit draft={draft} className="h-[520px]" />
      <Footer onBack={onBack}><Button onClick={onNext}>Next <ArrowRight className="size-4" /></Button></Footer>
    </CardContent></Card>
  );
}

function FinishStep({ path, modelId, onBack }: { path: ProjectPath; modelId: string; onBack: () => void }) {
  const snapshot = useSnapshot(path, modelId);
  const [, navigate] = useLocation();
  const s = snapshot.data;
  const open = s?.questions.filter((q) => q.data.status === "open").length ?? 0;
  return (
    <Card><CardContent className="space-y-4 pt-6">
      {s ? (
        <div className="grid grid-cols-3 gap-3 text-center">
          {[["Requirements", s.requirements.length], ["Definitions", s.definitions.length], ["Open questions", open]].map(([label, n]) => (
            <div key={label as string} className="rounded-md border p-3">
              <div className="text-2xl font-semibold">{n}</div>
              <div className="text-xs text-muted-foreground">{label}</div>
            </div>
          ))}
        </div>
      ) : <Loader2 className="size-4 animate-spin" />}
      <p className="text-sm text-muted-foreground">
        Everything the agent recorded is unconfirmed until someone with approval rights confirms it in the Registers tab.
        You can keep talking to the agent from the model editor.
      </p>
      <Footer onBack={onBack}>
        <Button variant="outline" disabled title="Available once the Architect agent is built (P4)">
          <Sparkles className="size-4" /> Generate draft model
        </Button>
        <Button onClick={() => navigate(`/t/${path.tenant_id}/p/${path.project_id}/m/${modelId}`)}>
          Open model <ArrowRight className="size-4" />
        </Button>
      </Footer>
    </CardContent></Card>
  );
}
