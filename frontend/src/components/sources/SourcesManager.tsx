import { AlertTriangle, CheckCircle2, Database, KeyRound, Loader2, Plus, Power, RefreshCw, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import {
  useCreateSource,
  useLinkModelSource,
  useModelSources,
  useSourceAction,
  useSources,
  useUpdateSource,
  type ProjectPath,
} from "@/api/hooks";
import { Field } from "@/components/editor/fields";
import { useConfirm } from "@/components/editor/useConfirm";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

const msg = (err: unknown) => (err instanceof Error ? err.message : "Something went wrong");

/**
 * Project data sources. Credentials are entered here (never in chat), verified to be
 * read-only before they're stored, encrypted at rest, and never shown again.
 * With a modelId, each source can also be linked to that model for the agents to use.
 */
export function SourcesManager({ path, role, modelId }: { path: ProjectPath; role: string | undefined; modelId?: string }) {
  const sources = useSources(path);
  const isOwner = role === "owner";
  const canEdit = role !== undefined && role !== "viewer";
  const [adding, setAdding] = useState(false);

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground flex gap-1.5">
        <ShieldCheck className="size-4 shrink-0" />
        Accounts must be read-only (SELECT only). Each one is checked when saved and before every use.
        Passwords are encrypted and never shown again. Don't paste credentials into the chat.
      </p>
      {sources.data?.length === 0 && <p className="text-sm text-muted-foreground">No data sources yet.</p>}
      <ul className="space-y-2">
        {sources.data?.map((s) => (
          <SourceRow key={s.id} path={path} source={s} isOwner={isOwner} canEdit={canEdit} modelId={modelId} />
        ))}
      </ul>
      {isOwner && (adding ? (
        <AddSource path={path} modelId={modelId} onDone={() => setAdding(false)} />
      ) : (
        <Button size="sm" variant="outline" onClick={() => setAdding(true)}><Plus className="size-4" /> Add data source</Button>
      ))}
      {!isOwner && <p className="text-xs text-muted-foreground">Only project owners can add or change data sources.</p>}
    </div>
  );
}

type Source = NonNullable<ReturnType<typeof useSources>["data"]>[number];

function SourceRow({ path, source, isOwner, canEdit, modelId }: {
  path: ProjectPath; source: Source; isOwner: boolean; canEdit: boolean; modelId?: string;
}) {
  const test = useSourceAction(path, "test");
  const disable = useSourceAction(path, "disable");
  const update = useUpdateSource(path);
  const [ask, confirmDialog] = useConfirm();
  const [rotating, setRotating] = useState(false);
  const [password, setPassword] = useState("");
  const check = source.privilege_check as { read_only?: boolean; warnings?: string[]; problems?: string[]; tables?: number } | null;
  const disabled = !!source.disabled_at;

  return (
    <li className={cn("rounded-md border p-3 space-y-2", disabled && "opacity-60")}>
      {confirmDialog}
      <div className="flex items-center gap-2 text-sm">
        <Database className="size-4 text-muted-foreground shrink-0" />
        <span className="min-w-0 flex-1">
          <span className="block font-medium truncate">{source.name}</span>
          <span className="block text-xs text-muted-foreground truncate" title={`${source.username}@${source.host}:${source.port}/${source.database}`}>
            {source.username}@{source.host}:{source.port}/{source.database}
          </span>
        </span>
        <span className="shrink-0 flex items-center gap-1.5">
          {modelId && !disabled && <LinkToggle path={path} modelId={modelId} sourceId={source.id} canEdit={canEdit} />}
          {disabled ? <Badge variant="outline">disabled</Badge>
            : check?.read_only ? <Badge variant="outline" className="gap-1 text-green-400"><CheckCircle2 className="size-3" /> read-only</Badge>
            : <Badge variant="outline" className="gap-1 text-red-400"><AlertTriangle className="size-3" /> not verified</Badge>}
        </span>
      </div>
      {check?.problems?.map((p, i) => <p key={i} className="text-xs text-red-400">{p}</p>)}
      {check?.warnings?.map((w, i) => <p key={i} className="text-xs text-amber-500">{w}</p>)}
      {source.last_verified_at && (
        <p className="text-xs text-muted-foreground">
          Checked {new Date(source.last_verified_at).toLocaleString()}{check?.tables !== undefined && ` · ${check.tables} tables`}
        </p>
      )}
      {!disabled && (
        <div className="flex flex-wrap gap-2">
          {canEdit && (
            <Button size="sm" variant="ghost" className="h-7" disabled={test.isPending}
              onClick={() => test.mutate(source.id, {
                onSuccess: () => toast.success(`${source.name}: connected, read-only`),
                onError: (e) => toast.error(msg(e)),
              })}>
              {test.isPending ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />} Test
            </Button>
          )}
          {isOwner && (
            <>
              <Button size="sm" variant="ghost" className="h-7" onClick={() => setRotating(!rotating)}>
                <KeyRound className="size-3.5" /> Change password
              </Button>
              <Button size="sm" variant="ghost" className="h-7 text-muted-foreground"
                onClick={async () => {
                  const ok = await ask({
                    title: `Disable ${source.name}?`,
                    description: "Agents and investigations stop using it. It's kept, with its evidence, so past findings stay traceable.",
                    confirmLabel: "Disable", destructive: true,
                  });
                  if (ok !== null) disable.mutate(source.id, { onError: (e) => toast.error(msg(e)) });
                }}>
                <Power className="size-3.5" /> Disable
              </Button>
            </>
          )}
        </div>
      )}
      {rotating && (
        <form className="flex gap-2" onSubmit={async (e) => {
          e.preventDefault();
          try {
            await update.mutateAsync({ id: source.id, body: { password } });
            toast.success("Password changed and verified");
            setPassword(""); setRotating(false);
          } catch (err) {
            toast.error(msg(err));  // the old password stays in place
          }
        }}>
          <Input type="password" autoComplete="new-password" className="h-8" placeholder="New password" value={password}
            onChange={(e) => setPassword(e.target.value)} />
          <Button size="sm" type="submit" disabled={!password || update.isPending}>
            {update.isPending && <Loader2 className="size-4 animate-spin" />} Verify and save
          </Button>
        </form>
      )}
    </li>
  );
}

function LinkToggle({ path, modelId, sourceId, canEdit }: { path: ProjectPath; modelId: string; sourceId: string; canEdit: boolean }) {
  const linked = useModelSources(path, modelId);
  const link = useLinkModelSource(path, modelId);
  const isLinked = !!linked.data?.some((s) => s.id === sourceId);
  return (
    <label className="flex items-center gap-1.5 text-xs text-muted-foreground mr-1">
      <input type="checkbox" checked={isLinked} disabled={!canEdit || link.isPending}
        onChange={(e) => link.mutate({ connectionId: sourceId, linked: e.target.checked }, { onError: (err) => toast.error(msg(err)) })} />
      Used by this model
    </label>
  );
}

function AddSource({ path, modelId, onDone }: { path: ProjectPath; modelId?: string; onDone: () => void }) {
  const create = useCreateSource(path);
  const link = useLinkModelSource(path, modelId ?? "");
  const [form, setForm] = useState({ name: "", host: "127.0.0.1", port: "3306", database: "", username: "", password: "" });
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });

  const save = async () => {
    try {
      const conn = await create.mutateAsync({ ...form, port: Number(form.port) || 3306 });
      if (modelId) await link.mutateAsync({ connectionId: conn.id, linked: true });
      toast.success(`${conn.name}: connected and verified read-only${modelId ? ", linked to this model" : ""}`);
      onDone();
    } catch (err) {
      toast.error(msg(err));  // nothing was stored
    }
  };

  return (
    <div className="rounded-md border p-3 grid grid-cols-2 gap-3">
      <Field label="Name"><Input value={form.name} onChange={set("name")} placeholder="e.g. Northstar operations" /></Field>
      <Field label="Database"><Input value={form.database} onChange={set("database")} /></Field>
      <Field label="Host"><Input value={form.host} onChange={set("host")} /></Field>
      <Field label="Port"><Input value={form.port} onChange={set("port")} /></Field>
      <Field label="Read-only user"><Input value={form.username} onChange={set("username")} autoComplete="off" /></Field>
      <Field label="Password"><Input type="password" autoComplete="new-password" value={form.password} onChange={set("password")} /></Field>
      <div className="col-span-2 flex gap-2">
        <Button size="sm" onClick={save} disabled={!form.name || !form.database || !form.username || create.isPending}>
          {create.isPending && <Loader2 className="size-4 animate-spin" />} Verify and save
        </Button>
        <Button size="sm" variant="ghost" onClick={onDone}>Cancel</Button>
      </div>
      <p className="col-span-2 text-xs text-muted-foreground">
        We connect, confirm the account can only read, and count the tables before saving. Nothing is stored if the check fails.
      </p>
    </div>
  );
}
