import { ArrowLeft, Eye, LayoutGrid, Plus } from "lucide-react";
import { useCallback, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { Link, useParams } from "wouter";
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
  const [tab, setTab] = useState("inspect");
  const [connecting, setConnecting] = useState<{ from: string; to: string } | null>(null);
  const [addingTable, setAddingTable] = useState(false);
  const [localLayout, setLocalLayout] = useState<Positions>({});

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

  if (snapshot.error) {
    return <div className="p-8 text-muted-foreground">This model doesn't exist or you don't have access to it.</div>;
  }
  if (!snap) return <div className="p-8 text-muted-foreground">Loading…</div>;

  const model = models.data?.find((m) => m.id === modelId);
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

        <aside className="w-[400px] border-l flex flex-col min-h-0">
          <Tabs value={tab} onValueChange={setTab} className="flex-1 flex flex-col min-h-0 gap-0">
            <TabsList className="m-2">
              <TabsTrigger value="inspect">Inspector</TabsTrigger>
              <TabsTrigger value="registers">Registers</TabsTrigger>
              <TabsTrigger value="versions">Versions</TabsTrigger>
            </TabsList>
            <ScrollArea className="flex-1 min-h-0">
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
                      <RecordActivity path={path} recordId={selectedTable.id} />
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
                      <RecordActivity path={path} recordId={selectedRel.id} />
                    </>
                  )}
                  {selection?.kind === "record" && <RecordActivity path={path} recordId={selection.id} />}
                </TabsContent>
                <TabsContent value="registers">
                  <RegistersPanel snapshot={snap} onSelectRecord={(id) => { setSelection({ kind: "record", id }); setTab("inspect"); }} />
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
