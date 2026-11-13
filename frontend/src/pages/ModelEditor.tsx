import { ArrowLeft, Eye, LayoutGrid, Plus, Sparkles } from "lucide-react";
import { useCallback, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { Link, useParams } from "wouter";
import { AssistantPanel } from "@/components/assistant/AssistantPanel";
import { ElementTrace } from "@/components/design/ElementTrace";
import { ReviewPanel } from "@/components/design/ReviewPanel";
import type { AgentName } from "@/api/hooks";
import { buildTrace } from "@/lib/trace";
import { SourcesManager } from "@/components/sources/SourcesManager";
import type { Schemas } from "@/api/client";
import { useModels, useProject, useSetLayout, useSnapshot } from "@/api/hooks";
import ModelCanvas, { type ModelCanvasHandle } from "@/components/canvas/ModelCanvas";
import { EnumSelect, Field } from "@/components/editor/fields";
import { NewRelationshipDialog } from "@/components/editor/NewRelationshipDialog";
import { RecordActivity } from "@/components/editor/RecordActivity";
import { RegistersPanel } from "@/components/editor/RegistersPanel";
import { RelationshipInspector } from "@/components/editor/RelationshipInspector";
import { TableInspector } from "@/components/editor/TableInspector";
import { useWrite } from "@/components/editor/useWrite";
import { VersionsPanel } from "@/components/editor/VersionsPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { STATE_LABELS, TABLE_KINDS } from "@/lib/labels";

type Selection = { kind: "table" | "relationship" | "record"; id: string } | null;
type Positions = Record<string, { x: number; y: number }>;

export default function ModelEditor() {
  const { tenantId, projectId, modelId } = useParams<{ tenantId: string; projectId: string; modelId: string }>();
  const path = useMemo(() => ({ tenant_id: tenantId, project_id: projectId }), [tenantId, projectId]);

  const [viewingSeq, setViewingSeq] = useState<number | undefined>(undefined);
  const snapshot = useSnapshot(path, modelId, viewingSeq);
  const project = useProject(path);
  const models = useModels(path);
  const setLayout = useSetLayout(path, modelId);
  const { write } = useWrite(path, modelId);
  const canvas = useRef<ModelCanvasHandle>(null);

  const [selection, setSelection] = useState<Selection>(null);
  const [tab, setTab] = useState("assistant");
  const [connecting, setConnecting] = useState<{ from: string; to: string } | null>(null);
  const [addingTable, setAddingTable] = useState(false);
  const [localLayout, setLocalLayout] = useState<Positions>({});
  const [agent, setAgent] = useState<AgentName>("business_requirements");
  const [focusCandidate, setFocusCandidate] = useState<string | undefined>(undefined);

  const role = project.data?.role;
  const canEdit = role !== undefined && role !== "viewer";
  const canApprove = role === "approver" || role === "owner";
  const readOnly = !canEdit || viewingSeq !== undefined;

  const snap = snapshot.data;
  const tables = useMemo(() => new Map(snap?.tables.map((t) => [t.id, t]) ?? []), [snap]);
  const attributes = useMemo(() => new Map(snap?.attributes.map((a) => [a.id, a]) ?? []), [snap]);
  const layout = useMemo(() => {
    const saved: Positions = Object.fromEntries((snap?.layout ?? []).map((p) => [p.record_id, { x: p.x, y: p.y }]));
    return { ...saved, ...localLayout };
  }, [snap, localLayout]);

  const savePositions = useCallback((positions: Positions) => {
    setLocalLayout((l) => ({ ...l, ...positions }));
    setLayout.mutate(
      Object.entries(positions).map(([record_id, p]) => ({ record_id, x: p.x, y: p.y })),
      { onError: (e) => toast.error(`Layout not saved: ${e.message}`) },
    );
  }, [setLayout]);

  const onSelectTable = useCallback((id: string | null) => {
    setSelection(id ? { kind: "table", id } : null);
    if (id) setTab("inspect");
  }, []);
  const onSelectRelationship = useCallback((id: string) => {
    setSelection({ kind: "relationship", id });
    setTab("inspect");
  }, []);
  const trace = useMemo(() => (snap ? buildTrace(snap) : null), [snap]);
  // Any record reference (chips in chat, registers, review): model elements open in the
  // inspector and on the canvas, register entries show their history and impact.
  const selectRecord = useCallback((id: string) => {
    if (tables.has(id)) return onSelectTable(id);
    if (snap?.relationships.some((r) => r.id === id)) return onSelectRelationship(id);
    const attr = attributes.get(id);
    if (attr) return onSelectTable(attr.data.table_id);
    setSelection({ kind: "record", id });
    setTab("inspect");
  }, [tables, attributes, snap, onSelectTable, onSelectRelationship]);
  const onCandidate = useCallback((id: string) => {
    setFocusCandidate(id);
    toast.info("The Architect's design and the Validator's review are on the Review tab.", {
      action: { label: "Open", onClick: () => setTab("review") },
    });
  }, []);

  if (snapshot.error) {
    return <div className="p-8 text-muted-foreground">This model doesn't exist or you don't have access to it.</div>;
  }
  if (!snap) return <div className="p-8 text-muted-foreground">Loading…</div>;

  const model = models.data?.find((m) => m.id === modelId);
  // Relationships have no name, so describe them by their ends for the impact list.
  const describe = (id: string) => {
    const rel = snap.relationships.find((r) => r.id === id);
    if (!rel) return undefined;
    const from = tables.get(rel.data.from_table_id)?.data.name ?? "?";
    const to = tables.get(rel.data.to_table_id)?.data.name ?? "?";
    return `${from} → ${to}${rel.data.role_name ? ` (${rel.data.role_name})` : ""}`;
  };
  const selectedTable = selection?.kind === "table" ? tables.get(selection.id) : undefined;
  const selectedRel = selection?.kind === "relationship" ? snap.relationships.find((r) => r.id === selection.id) : undefined;

  return (
    <div className="h-screen flex flex-col">
      <header className="border-b px-4 py-2 flex items-center gap-3">
        <Link href={`/t/${tenantId}/p/${projectId}`} className="text-muted-foreground hover:text-foreground">
          <ArrowLeft className="size-4" />
        </Link>
        <h1 className="font-semibold">{model?.name ?? "Model"}</h1>
        <Badge variant="secondary">{STATE_LABELS[snap.state.effective_state]}</Badge>
        <span className="text-xs font-mono text-muted-foreground">seq {snap.as_of_seq}</span>
        {viewingSeq !== undefined && (
          <Badge variant="outline" className="gap-1 border-amber-500/40 text-amber-400">
            <Eye className="size-3" /> Viewing seq {viewingSeq} (read-only)
            <button type="button" className="underline ml-1" onClick={() => setViewingSeq(undefined)}>back to latest</button>
          </Badge>
        )}
        {!canEdit && role && <Badge variant="outline">read-only ({role})</Badge>}
        <div className="ml-auto flex gap-2">
          {!readOnly && (
            <Button size="sm" variant="outline" onClick={() => { setAgent("architect"); setTab("assistant"); }}>
              <Sparkles className="size-4" /> Architect
            </Button>
          )}
          {!readOnly && (
            <Button size="sm" variant="outline" onClick={() => setAddingTable(true)}>
              <Plus className="size-4" /> Table
            </Button>
          )}
          <Button size="sm" variant="outline" disabled={readOnly || snap.tables.length === 0}
            onClick={() => { savePositions(canvas.current?.autoLayout() ?? {}); setTimeout(() => canvas.current?.fitView(), 50); }}>
            <LayoutGrid className="size-4" /> Auto layout
          </Button>
        </div>
      </header>

      <div className="flex-1 flex min-h-0">
        <div className="flex-1 min-w-0">
          {snap.tables.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center gap-3 text-muted-foreground">
              <p>This model has no tables yet.</p>
              {!readOnly && <Button onClick={() => setAddingTable(true)}><Plus className="size-4" /> Add the first table</Button>}
            </div>
          ) : (
            <ModelCanvas
              ref={canvas}
              tables={snap.tables}
              attributes={snap.attributes}
              relationships={snap.relationships}
              savedLayout={layout}
              readOnly={readOnly}
              selectedTableId={selectedTable?.id ?? null}
              selectedRelationshipId={selectedRel?.id ?? null}
              onSelectTable={onSelectTable}
              onSelectRelationship={onSelectRelationship}
              onMoveTable={(id, x, y) => savePositions({ [id]: { x, y } })}
              onConnectTables={(from, to) => setConnecting(orientRelationship(tables, from, to))}
            />
          )}
        </div>

        <aside className="w-[440px] shrink-0 border-l flex flex-col min-h-0 overflow-hidden">
          <Tabs value={tab} onValueChange={setTab} className="flex-1 flex flex-col min-h-0 gap-0">
            {/* Six tabs in a 440px panel: compact triggers so they all fit. */}
            <TabsList className="m-2 w-[calc(100%-1rem)] [&>button]:px-1.5 [&>button]:text-xs">
              <TabsTrigger value="assistant">Assistant</TabsTrigger>
              <TabsTrigger value="inspect">Inspector</TabsTrigger>
              <TabsTrigger value="registers">Registers</TabsTrigger>
              <TabsTrigger value="review">Review</TabsTrigger>
              <TabsTrigger value="sources">Sources</TabsTrigger>
              <TabsTrigger value="versions">Versions</TabsTrigger>
            </TabsList>
            {/* The assistant scrolls its own transcript and keeps its composer pinned. */}
            <TabsContent value="assistant" className="flex-1 min-h-0 flex flex-col px-4 pb-4 gap-2">
              <div className="flex rounded-md border p-0.5 text-xs w-fit" role="tablist" aria-label="Agent">
                {([["business_requirements", "Requirements"], ["architect", "Architect"]] as const).map(([value, label]) => (
                  <button key={value} type="button" role="tab" aria-selected={agent === value}
                    className={`px-2.5 py-1 rounded ${agent === value ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground"}`}
                    onClick={() => setAgent(value)}>{label}</button>
                ))}
              </div>
              {/* Both stay mounted so a running agent keeps streaming while you switch. */}
              {(["business_requirements", "architect"] as const).map((a) => (
                <AssistantPanel
                  key={a}
                  agent={a}
                  path={path}
                  modelId={modelId}
                  canEdit={canEdit && viewingSeq === undefined}
                  className={agent === a ? "flex-1" : "hidden"}
                  onSelectRecord={selectRecord}
                  onCandidate={onCandidate}
                />
              ))}
            </TabsContent>
            {/* Radix wraps scroll content in a display:table div that grows to the widest child;
                make it a block so panel content wraps to the panel's width. */}
            <ScrollArea className={tab === "assistant" ? "hidden" : "flex-1 min-h-0 [&_[data-slot=scroll-area-viewport]>div]:!block"}>
              <div className="p-4">
                <TabsContent value="inspect" className="space-y-6">
                  {!selection && <p className="text-sm text-muted-foreground">Select a table or relationship. Drag from a table's bottom handle to another table to relate them.</p>}
                  {selectedTable && (
                    <>
                      <TableInspector
                        table={selectedTable}
                        attributes={snap.attributes.filter((a) => a.data.table_id === selectedTable.id)}
                        readOnly={readOnly}
                        write={write}
                        onDeleted={() => setSelection(null)}
                      />
                      {trace && <ElementTrace trace={trace} onSelect={selectRecord}
                        ids={[selectedTable.id, ...snap.attributes.filter((a) => a.data.table_id === selectedTable.id).map((a) => a.id)]} />}
                      <RecordActivity path={path} recordId={selectedTable.id} describe={describe} />
                    </>
                  )}
                  {selectedRel && (
                    <>
                      <RelationshipInspector
                        relationship={selectedRel}
                        tables={tables}
                        attributes={attributes}
                        readOnly={readOnly}
                        write={write}
                        onDeleted={() => setSelection(null)}
                      />
                      {trace && <ElementTrace trace={trace} ids={[selectedRel.id]} onSelect={selectRecord} />}
                      <RecordActivity path={path} recordId={selectedRel.id} describe={describe} />
                    </>
                  )}
                  {selection?.kind === "record" && <RecordActivity path={path} recordId={selection.id} describe={describe} />}
                </TabsContent>
                <TabsContent value="registers">
                  <RegistersPanel snapshot={snap} canApprove={canApprove && viewingSeq === undefined} write={write}
                    onSelectRecord={selectRecord} />
                </TabsContent>
                <TabsContent value="review">
                  <ReviewPanel path={path} modelId={modelId} snapshot={snap} canEdit={canEdit && viewingSeq === undefined}
                    focusCandidateId={focusCandidate} onSelectRecord={selectRecord} />
                </TabsContent>
                <TabsContent value="sources">
                  <SourcesManager path={path} role={role} modelId={modelId} />
                </TabsContent>
                <TabsContent value="versions">
                  <VersionsPanel path={path} modelId={modelId} snapshot={snap} canEdit={canEdit} canApprove={canApprove}
                    viewingSeq={viewingSeq} onView={(seq) => { setViewingSeq(seq); setSelection(null); }} />
                </TabsContent>
              </div>
            </ScrollArea>
          </Tabs>
        </aside>
      </div>

      {connecting && tables.get(connecting.from) && tables.get(connecting.to) && (
        <NewRelationshipDialog
          from={tables.get(connecting.from)!}
          to={tables.get(connecting.to)!}
          attributes={snap.attributes}
          write={write}
          onSwap={() => setConnecting({ from: connecting.to, to: connecting.from })}
          onClose={() => setConnecting(null)}
        />
      )}
      {addingTable && (
        <AddTableDialog
          onClose={() => setAddingTable(false)}
          onCreate={async (data) => {
            const result = await write([{ op: "create", type: "table", ref: "t", data }], { success: "Table created" });
            if (result) {
              setAddingTable(false);
              // Put the new table where the user is looking. The first table has no canvas
              // yet, so auto-layout places it.
              const center = canvas.current?.visibleCenter();
              if (center) savePositions({ [result.created.t]: center });
              onSelectTable(result.created.t);
            }
          }}
        />
      )}
    </div>
  );
}

/**
 * Decide which table holds the foreign key. In a star schema the fact (or bridge) references
 * the dimension, whichever way the user dragged. Between tables of the same kind, such as a
 * dimension referencing an outrigger, the drag source holds the key.
 */
export function orientRelationship(
  tables: Map<string, { data: { kind: string } }>,
  source: string,
  target: string,
): { from: string; to: string } {
  const s = tables.get(source)?.data.kind;
  const t = tables.get(target)?.data.kind;
  if (s === "dimension" && (t === "fact" || t === "bridge")) return { from: target, to: source };
  return { from: source, to: target };
}

function AddTableDialog({ onClose, onCreate }: { onClose: () => void; onCreate: (d: Schemas["TableData"]) => Promise<void> }) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState<Schemas["TableData"]["kind"]>("dimension");
  const [grain, setGrain] = useState("");
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-sm">
        <DialogHeader><DialogTitle>New table</DialogTitle></DialogHeader>
        <form id="add-table" className="space-y-3"
          onSubmit={(e) => { e.preventDefault(); if (name.trim()) onCreate({ name: name.trim(), kind, grain: grain.trim() || null, scd_type: "none" }); }}>
          <Field label="Name"><Input autoFocus className="h-8" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. dim_customer" /></Field>
          <Field label="Kind"><EnumSelect value={kind} options={TABLE_KINDS} onChange={(v) => v && setKind(v)} /></Field>
          {kind === "fact" && (
            <Field label="Grain"><Input className="h-8" value={grain} onChange={(e) => setGrain(e.target.value)} placeholder="One row per order line" /></Field>
          )}
        </form>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="add-table" disabled={!name.trim()}>Create</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
