import { ArrowLeft, ArrowRight, Check, Database, FileText, Flag, Loader2, MessageSquare, Sparkles, Upload } from "lucide-react";
import { useRef, useState } from "react";
import { toast } from "sonner";
import { Link, useLocation, useParams } from "wouter";
import { uploadDocument } from "@/api/agent";
import {
  useCreateModel,
  useDocuments,
  useModelSources,
  useProject,
  useSnapshot,
  type ProjectPath,
} from "@/api/hooks";
import { SourcesManager } from "@/components/sources/SourcesManager";
import { useFlagCounts } from "@/components/runs/FlaggedContent";
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
      {step === 2 && modelId && <SourceStep path={path} modelId={modelId} role={project.data?.role} onBack={() => setStep(1)} onNext={() => setStep(3)} />}
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
  const flagCounts = useFlagCounts(path);
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
            {flagCounts.byDocument.get(d.id) && (
              <Badge variant="outline" className="gap-1 font-normal text-amber-500" title="An agent found text here that tried to instruct it; see the Review tab">
                <Flag className="size-3" /> flagged
              </Badge>
            )}
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

function SourceStep({ path, modelId, role, onBack, onNext }: {
  path: ProjectPath; modelId: string; role: string | undefined; onBack: () => void; onNext: () => void;
}) {
  const linked = useModelSources(path, modelId);
  return (
    <Card><CardContent className="space-y-4 pt-6">
      <p className="text-sm text-muted-foreground">
        Optional. A read-only connection to a MySQL source lets the Architect check what the data can support when it designs
        the model: tables, keys, grain and data checks. The Business Requirements agent in the next step works from documents
        and conversation, so its feasibility notes stay provisional until then. You can skip this and add a source later from
        the model's Sources menu.
      </p>
      <SourcesManager path={path} role={role} modelId={modelId} />
      <Footer onBack={onBack}>
        <Button variant={linked.data?.length ? "default" : "outline"} onClick={onNext}>
          {linked.data?.length ? "Next" : "Skip"} <ArrowRight className="size-4" />
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
  const linked = useModelSources(path, modelId);
  const [, navigate] = useLocation();
  const s = snapshot.data;
  const open = s?.questions.filter((q) => q.data.status === "open") ?? [];
  const blocking = open.filter((q) => q.data.is_blocking).length;
  const sources = linked.data?.length;
  const editor = `/t/${path.tenant_id}/p/${path.project_id}/m/${modelId}`;
  const canDraft = !!s && s.requirements.length > 0;
  return (
    <Card><CardContent className="space-y-4 pt-6">
      {s ? (
        <div className="grid grid-cols-4 gap-3 text-center">
          {([["Requirements", s.requirements.length], ["Definitions", s.definitions.length],
             ["Open questions", open.length], ["Linked sources", sources ?? "…"]] as const).map(([label, n]) => (
            <div key={label} className="rounded-md border p-3">
              <div className="text-2xl font-semibold">{n}</div>
              <div className="text-xs text-muted-foreground">{label}</div>
            </div>
          ))}
        </div>
      ) : <Loader2 className="size-4 animate-spin" />}
      <div className="text-sm text-muted-foreground space-y-2">
        <p>
          Next, the Architect designs a draft Kimball model from these requirements
          {sources ? " and checks it against the linked source" : ""}, and the Validator reviews it. It takes a few
          minutes; you'll follow it in the editor's AI Assistant, and the tables appear on the canvas when it's done.
        </p>
        {s && s.requirements.length === 0 && (
          <p className="text-amber-500">There are no requirements yet, so there's nothing to design from. Go back and talk to the Requirements agent.</p>
        )}
        {open.length > 0 && (
          <p>
            {open.length} question{open.length === 1 ? " is" : "s are"} still open{blocking ? ` (${blocking} blocking)` : ""}. The
            Architect designs around them and lists them as blockers; you can also go back and answer them first.
          </p>
        )}
        {sources === 0 && (
          <p>No source is linked, so the design will be unverified against data. You can link one in the Source step.</p>
        )}
        <p>Nothing is confirmed or accepted until a person does it, in the editor's Registers and Review.</p>
      </div>
      <Footer onBack={onBack}>
        <Button variant="outline" onClick={() => navigate(editor)}>
          Open model without a draft
        </Button>
        <Button disabled={!canDraft} onClick={() => navigate(`${editor}?draft=1`)}>
          <Sparkles className="size-4" /> Generate draft model <ArrowRight className="size-4" />
        </Button>
      </Footer>
    </CardContent></Card>
  );
}
