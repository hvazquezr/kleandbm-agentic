import { Bot, ClipboardList, Database, Eye, History, LayoutGrid, Maximize, MoreVertical, PanelRightClose, PanelRightOpen, Pencil, Plus, ShieldCheck, Sparkles, Trash2 } from "lucide-react";
import { useCallback, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { useLocation, useParams } from "wouter";
import { AssistantPanel } from "@/components/assistant/AssistantPanel";
import { ElementTrace } from "@/components/design/ElementTrace";
import { ReviewPanel } from "@/components/design/ReviewPanel";
import type { AgentName } from "@/api/hooks";
import { buildTrace } from "@/lib/trace";
import { SourcesManager } from "@/components/sources/SourcesManager";
import type { Schemas, Snapshot } from "@/api/client";
import { useModels, useProject, useSetLayout, useSnapshot } from "@/api/hooks";
import ModelCanvas, { type CanvasMenuTarget, type ModelCanvasHandle } from "@/components/canvas/ModelCanvas";
import { Drawer, LogoPill, Pill, TablesPanel, ToolButton, usePersistentFlag } from "@/components/editor/CanvasChrome";
import { EnumSelect, Field } from "@/components/editor/fields";
import { NewRelationshipDialog } from "@/components/editor/NewRelationshipDialog";
import { RecordActivity } from "@/components/editor/RecordActivity";
import { RegistersPanel, type RegisterKey } from "@/components/editor/RegistersPanel";
import { RelationshipInspector } from "@/components/editor/RelationshipInspector";
import { TableInspector } from "@/components/editor/TableInspector";
import { useConfirm } from "@/components/editor/useConfirm";
import { useWrite } from "@/components/editor/useWrite";
import { VersionsPanel } from "@/components/editor/VersionsPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { STATE_LABELS, TABLE_KINDS } from "@/lib/labels";

type Selection = { kind: "table" | "relationship" | "record"; id: string } | null;
type Positions = Record<string, { x: number; y: number }>;
type DrawerName = "assistant" | "review" | "versions";

const DRAWER_WIDTH: Record<DrawerName, number> = { assistant: 440, review: 480, versions: 380 };

export default function ModelEditor() {
  const { tenantId, projectId, modelId } = useParams<{ tenantId: string; projectId: string; modelId: string }>();
  const path = useMemo(() => ({ tenant_id: tenantId, project_id: projectId }), [tenantId, projectId]);
  const projectHref = `/t/${tenantId}/p/${projectId}`;
  const [, navigate] = useLocation();

  const [viewingSeq, setViewingSeq] = useState<number | undefined>(undefined);
  const snapshot = useSnapshot(path, modelId, viewingSeq);
  const project = useProject(path);
  const models = useModels(path);
  const setLayout = useSetLayout(path, modelId);
  const { write } = useWrite(path, modelId);
  const canvas = useRef<ModelCanvasHandle>(null);

  // Selection highlights an element on the canvas; `editing` is the element whose editor is open.
  const [selection, setSelection] = useState<Selection>(null);
  const [editing, setEditing] = useState<Selection>(null);
  // One right drawer at a time. The assistant panels stay mounted so a run keeps streaming.
  const [drawer, setDrawer] = useState<DrawerName | null>(null);
  const [tablesOpen, setTablesOpen] = usePersistentFlag("kleandbm.tablesPanel", true);
  const [registersOpen, setRegistersOpen] = useState<RegisterKey | null>(null);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [connecting, setConnecting] = useState<{ from: string; to: string } | null>(null);
  // Where a new table goes: a canvas position from a right-click, or null for the visible centre.
  const [addingTable, setAddingTable] = useState<{ at: { x: number; y: number } | null } | null>(null);
  const [menu, setMenu] = useState<{ target: CanvasMenuTarget; x: number; y: number } | null>(null);
  const [ask, confirmDialog] = useConfirm();
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
  const attributeCounts = useMemo(() => {
    const m = new Map<string, number>();
    for (const a of snap?.attributes ?? []) m.set(a.data.table_id, (m.get(a.data.table_id) ?? 0) + 1);
    return m;
  }, [snap]);
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

  const onSelectTable = useCallback((id: string | null) => setSelection(id ? { kind: "table", id } : null), []);
  const openTable = useCallback((id: string) => {
    setSelection({ kind: "table", id });
    setEditing({ kind: "table", id });
  }, []);
  // Relationships are thin lines, hard to double-click, so one click opens their editor.
  const onSelectRelationship = useCallback((id: string) => {
    setSelection({ kind: "relationship", id });
    setEditing({ kind: "relationship", id });
  }, []);
  const trace = useMemo(() => (snap ? buildTrace(snap) : null), [snap]);
  // Any record reference (chips in chat, registers, review): model elements open their editor
  // and are found on the canvas; register entries open their history and impact.
  const selectRecord = useCallback((id: string) => {
    const attr = attributes.get(id);
    const tableId = tables.has(id) ? id : attr?.data.table_id;
    if (tableId) {
      openTable(tableId);
      canvas.current?.focusTable(tableId);
      return;
    }
    if (snap?.relationships.some((r) => r.id === id)) return onSelectRelationship(id);
    setEditing({ kind: "record", id });
  }, [tables, attributes, snap, openTable, onSelectRelationship]);
  const findTable = useCallback((id: string) => {
    setSelection({ kind: "table", id });
    canvas.current?.focusTable(id);
  }, []);
  const deleteTable = useCallback(async (id: string) => {
    const t = tables.get(id);
    if (!t) return;
    const ok = await ask({
      title: `Delete ${t.data.name}?`,
      description: "Its attributes and relationships are deleted too. The history keeps every earlier version.",
      confirmLabel: "Delete table",
      destructive: true,
    });
    if (ok !== null && await write([{ op: "delete", id, expected_seq: t.seq }], { success: "Table deleted" })) {
      setSelection(null);
    }
  }, [tables, ask, write]);
  const deleteRelationship = useCallback(async (id: string) => {
    const r = snap?.relationships.find((x) => x.id === id);
    if (!r) return;
    const ok = await ask({
      title: "Delete this relationship?",
      description: "The foreign key attribute is kept; delete it separately if it's no longer needed.",
      confirmLabel: "Delete relationship",
      destructive: true,
    });
    if (ok !== null && await write([{ op: "delete", id, expected_seq: r.seq }], { success: "Relationship deleted" })) {
      setSelection(null);
    }
  }, [snap, ask, write]);
  const onCanvasMenu = useCallback((target: CanvasMenuTarget, at: { x: number; y: number }) => {
    if (target.kind === "table") setSelection({ kind: "table", id: target.id });
    if (target.kind === "relationship") setSelection({ kind: "relationship", id: target.id });
    setMenu({ target, ...at });
  }, []);
  const toggleDrawer = (d: DrawerName) => setDrawer((cur) => (cur === d ? null : d));
  const onCandidate = useCallback((id: string) => {
    setFocusCandidate(id);
    toast.info("The Architect's design and the Validator's review are ready.", {
      action: { label: "Review", onClick: () => setDrawer("review") },
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
  const openBlocking = snap.questions.filter((q) => q.data.status === "open" && q.data.is_blocking).length;
  const state = snap.state.effective_state;

  return (
    <div className="h-screen flex overflow-hidden">
      {tablesOpen && (
        <TablesPanel
          projectHref={projectHref}
          projectName={project.data?.name}
          tables={snap.tables}
          attributeCounts={attributeCounts}
          selectedId={selectedTable?.id ?? null}
          onSelect={findTable}
          onOpen={openTable}
        />
      )}

      <main className="relative flex-1 min-w-0 group/canvas">
        {snap.tables.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center gap-3 text-muted-foreground">
            <p>This model has no tables yet.</p>
            {!readOnly && (
              <div className="flex gap-2">
                <Button onClick={() => { setAgent("architect"); setDrawer("assistant"); }}>
                  <Sparkles className="size-4" /> Ask the Architect
                </Button>
                <Button variant="outline" onClick={() => setAddingTable({ at: null })}><Plus className="size-4" /> Add a table</Button>
              </div>
            )}
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
            onOpenTable={openTable}
            onSelectRelationship={onSelectRelationship}
            onMoveTable={(id, x, y) => savePositions({ [id]: { x, y } })}
            onConnectTables={(from, to) => setConnecting(orientRelationship(tables, from, to))}
            onContextMenu={onCanvasMenu}
          />
        )}

        <div className="absolute top-4 left-4 z-10">
          <LogoPill open={tablesOpen} onToggle={() => setTablesOpen(!tablesOpen)} />
        </div>

        {(viewingSeq !== undefined || (!canEdit && role)) && (
          <div className="absolute top-4 left-1/2 -translate-x-1/2 z-10">
            <Pill className="px-3 text-xs">
              {viewingSeq !== undefined ? (
                <span className="flex items-center gap-1.5 text-amber-400">
                  <Eye className="size-3.5" /> Viewing seq {viewingSeq}, read-only
                  <button type="button" className="underline ml-1 text-foreground" onClick={() => setViewingSeq(undefined)}>back to latest</button>
                </span>
              ) : <span className="text-muted-foreground">Read-only ({role})</span>}
            </Pill>
          </div>
        )}

        <div className="absolute top-4 right-4 z-10">
          <Pill>
            <button type="button" onClick={() => toggleDrawer("review")} title="Open the review"
              className="px-1.5">
              <Badge variant="secondary" className={state === "ready_for_review" ? "text-green-400" : state === "accepted" ? "text-blue-300" : ""}>
                {STATE_LABELS[state]}
              </Badge>
            </button>
            <ToolButton label="Registers" badge={openBlocking} onClick={() => setRegistersOpen(openBlocking ? "questions" : "requirements")}>
              <ClipboardList className="size-4" />
            </ToolButton>
            <ToolButton label="Review" active={drawer === "review"} onClick={() => toggleDrawer("review")}>
              <ShieldCheck className="size-4" />
            </ToolButton>
            <ToolButton label="Versions" active={drawer === "versions"} onClick={() => toggleDrawer("versions")}>
              <History className="size-4" />
            </ToolButton>
            <Button size="sm" variant={drawer === "assistant" ? "secondary" : "outline"} className="h-8 gap-1.5"
              onClick={() => toggleDrawer("assistant")}>
              <Sparkles className="size-4" /> AI Assistant
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="sm" className="h-8 px-2" aria-label="Model menu"><MoreVertical className="size-4" /></Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-72">
                <ModelSummary name={model?.name} description={model?.description} snap={snap} />
                <DropdownMenuSeparator />
                {!readOnly && <DropdownMenuItem onSelect={() => setAddingTable({ at: null })}><Plus className="size-4" /> Add table</DropdownMenuItem>}
                <DropdownMenuItem disabled={readOnly || snap.tables.length === 0}
                  onSelect={() => { savePositions(canvas.current?.autoLayout() ?? {}); setTimeout(() => canvas.current?.fitView(), 50); }}>
                  <LayoutGrid className="size-4" /> Auto layout
                </DropdownMenuItem>
                <DropdownMenuItem disabled={snap.tables.length === 0} onSelect={() => canvas.current?.fitView()}>
                  <Maximize className="size-4" /> Fit to screen
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem onSelect={() => setSourcesOpen(true)}><Database className="size-4" /> Sources</DropdownMenuItem>
                <DropdownMenuItem onSelect={() => navigate(projectHref)}>Back to project</DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </Pill>
        </div>

        {snap.tables.length > 0 && (
          <div className="pointer-events-none absolute bottom-4 left-1/2 -translate-x-1/2 z-10 hidden group-hover/canvas:block">
            <Pill className="rounded-full px-3 text-xs text-muted-foreground">
              Double-click a table to edit it · right-click for more{readOnly ? "" : " · drag from a table's bottom handle to relate it"}
            </Pill>
          </div>
        )}
      </main>

      {/* The assistant drawer stays mounted (hidden) so a running agent keeps streaming. */}
      <Drawer
        hidden={drawer !== "assistant"}
        width={DRAWER_WIDTH.assistant}
        onClose={() => setDrawer(null)}
        title={<><Bot className="size-4" /> AI Assistant</>}
        actions={
          <div className="flex rounded-md border p-0.5 text-xs" role="tablist" aria-label="Agent">
            {([["business_requirements", "Requirements"], ["architect", "Architect"]] as const).map(([value, label]) => (
              <button key={value} type="button" role="tab" aria-selected={agent === value}
                className={`px-2 py-0.5 rounded ${agent === value ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground"}`}
                onClick={() => setAgent(value)}>{label}</button>
            ))}
          </div>
        }
      >
        <div className="flex-1 min-h-0 flex flex-col p-4">
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
        </div>
      </Drawer>
      {drawer === "review" && (
        <Drawer width={DRAWER_WIDTH.review} onClose={() => setDrawer(null)} title={<><ShieldCheck className="size-4" /> Review</>}>
          <div className="flex-1 min-h-0 overflow-y-auto p-4">
            <ReviewPanel path={path} modelId={modelId} snapshot={snap} canEdit={canEdit && viewingSeq === undefined}
              focusCandidateId={focusCandidate} onSelectRecord={selectRecord} />
          </div>
        </Drawer>
      )}
      {drawer === "versions" && (
        <Drawer width={DRAWER_WIDTH.versions} onClose={() => setDrawer(null)} title={<><History className="size-4" /> Versions</>}>
          <div className="flex-1 min-h-0 overflow-y-auto p-4">
            <VersionsPanel path={path} modelId={modelId} snapshot={snap} canEdit={canEdit} canApprove={canApprove}
              viewingSeq={viewingSeq} onView={(seq) => { setViewingSeq(seq); setSelection(null); setEditing(null); }} />
          </div>
        </Drawer>
      )}

      {confirmDialog}
      {menu && (
        <DropdownMenu open onOpenChange={(o) => !o && setMenu(null)}>
          <DropdownMenuTrigger asChild>
            <span aria-hidden className="fixed size-0" style={{ left: menu.x, top: menu.y }} />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-52">
            {menu.target.kind === "pane" && (() => {
              const at = menu.target.position;
              return (
                <>
                  {!readOnly && <DropdownMenuItem onSelect={() => setAddingTable({ at })}><Plus className="size-4" /> Add table here</DropdownMenuItem>}
                  {!readOnly && (
                    <DropdownMenuItem onSelect={() => { setAgent("architect"); setDrawer("assistant"); }}>
                      <Sparkles className="size-4" /> Ask the Architect
                    </DropdownMenuItem>
                  )}
                  {!readOnly && <DropdownMenuSeparator />}
                  <DropdownMenuItem disabled={readOnly}
                    onSelect={() => { savePositions(canvas.current?.autoLayout() ?? {}); setTimeout(() => canvas.current?.fitView(), 50); }}>
                    <LayoutGrid className="size-4" /> Auto layout
                  </DropdownMenuItem>
                  <DropdownMenuItem onSelect={() => canvas.current?.fitView()}><Maximize className="size-4" /> Fit to screen</DropdownMenuItem>
                </>
              );
            })()}
            {menu.target.kind === "table" && (() => {
              const id = menu.target.id;
              return (
                <>
                  <DropdownMenuLabel className="font-mono text-xs">{tables.get(id)?.data.name}</DropdownMenuLabel>
                  <DropdownMenuItem onSelect={() => openTable(id)}><Pencil className="size-4" /> {readOnly ? "Open table" : "Edit table"}</DropdownMenuItem>
                  {!readOnly && <DropdownMenuSeparator />}
                  {!readOnly && (
                    <DropdownMenuItem variant="destructive" onSelect={() => void deleteTable(id)}>
                      <Trash2 className="size-4" /> Delete table
                    </DropdownMenuItem>
                  )}
                </>
              );
            })()}
            {menu.target.kind === "relationship" && (() => {
              const id = menu.target.id;
              return (
                <>
                  <DropdownMenuLabel className="text-xs">{describe(id)}</DropdownMenuLabel>
                  <DropdownMenuItem onSelect={() => onSelectRelationship(id)}><Pencil className="size-4" /> {readOnly ? "Open relationship" : "Edit relationship"}</DropdownMenuItem>
                  {!readOnly && <DropdownMenuSeparator />}
                  {!readOnly && (
                    <DropdownMenuItem variant="destructive" onSelect={() => void deleteRelationship(id)}>
                      <Trash2 className="size-4" /> Delete relationship
                    </DropdownMenuItem>
                  )}
                </>
              );
            })()}
          </DropdownMenuContent>
        </DropdownMenu>
      )}

      <RegistersDialog
        open={registersOpen}
        onOpenChange={setRegistersOpen}
        snapshot={snap}
        canApprove={canApprove && viewingSeq === undefined}
        write={write}
        onSelectRecord={selectRecord}
      />

      <Dialog open={sourcesOpen} onOpenChange={setSourcesOpen}>
        <DialogContent className="sm:max-w-3xl max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Sources</DialogTitle>
            <DialogDescription>The read-only database connections this model's agents can investigate.</DialogDescription>
          </DialogHeader>
          <SourcesManager path={path} role={role} modelId={modelId} />
        </DialogContent>
      </Dialog>

      <ElementDialog
        editing={editing}
        onClose={() => setEditing(null)}
        snap={snap}
        tables={tables}
        attributes={attributes}
        readOnly={readOnly}
        write={write}
        path={path}
        describe={describe}
        trace={trace}
        onSelectRecord={selectRecord}
        onDeleted={() => { setEditing(null); setSelection(null); }}
      />

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
          onClose={() => setAddingTable(null)}
          onCreate={async (data) => {
            const result = await write([{ op: "create", type: "table", ref: "t", data }], { success: "Table created" });
            if (result) {
              setAddingTable(null);
              // Put the new table where the user is looking. The first table has no canvas
              // yet, so auto-layout places it.
              const center = addingTable.at ?? canvas.current?.visibleCenter();
              if (center) savePositions({ [result.created.t]: center });
              openTable(result.created.t);
            }
          }}
        />
      )}
    </div>
  );
}

function ModelSummary({ name, description, snap }: { name?: string; description?: string | null; snap: Snapshot }) {
  const facts = snap.tables.filter((t) => t.data.kind === "fact").length;
  const dims = snap.tables.filter((t) => t.data.kind === "dimension").length;
  const bridges = snap.tables.length - facts - dims;
  return (
    <DropdownMenuLabel className="font-normal space-y-2 py-2">
      <div>
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground">Model</div>
        <div className="font-medium text-sm">{name ?? "Model"}</div>
        {description && <p className="text-xs text-muted-foreground mt-1 line-clamp-3">{description}</p>}
      </div>
      <div className="rounded-md bg-muted/50 px-2 py-1.5 text-xs text-muted-foreground space-y-0.5">
        <div className="text-foreground">
          {snap.tables.length} tables <span className="text-muted-foreground">({facts} facts, {dims} dimensions{bridges ? `, ${bridges} bridges` : ""})</span>
        </div>
        <div>{snap.attributes.length} attributes · {snap.relationships.length} relationships</div>
        <div>{STATE_LABELS[snap.state.effective_state]} · seq {snap.as_of_seq}</div>
      </div>
    </DropdownMenuLabel>
  );
}

const REGISTERS: { key: RegisterKey; label: string; count: (s: Snapshot) => number }[] = [
  { key: "requirements", label: "Requirements", count: (s) => s.requirements.length },
  { key: "definitions", label: "Definitions", count: (s) => s.definitions.length },
  { key: "questions", label: "Questions", count: (s) => s.questions.length },
  { key: "decisions", label: "Decisions", count: (s) => s.decisions.length },
  { key: "checks", label: "Checks", count: (s) => s.checks.length },
];

function RegistersDialog({ open, onOpenChange, snapshot, canApprove, write, onSelectRecord }: {
  open: RegisterKey | null;
  onOpenChange: (v: RegisterKey | null) => void;
  snapshot: Snapshot;
  canApprove: boolean;
  write: ReturnType<typeof useWrite>["write"];
  onSelectRecord: (id: string) => void;
}) {
  return (
    <Dialog open={open !== null} onOpenChange={(o) => !o && onOpenChange(null)}>
      <DialogContent className="sm:max-w-5xl h-[85vh] flex flex-col gap-3">
        <DialogHeader>
          <DialogTitle>Registers</DialogTitle>
          <DialogDescription>Requirements, business definitions, questions, decisions and validation checks for this project.</DialogDescription>
        </DialogHeader>
        <Tabs value={open ?? "requirements"} onValueChange={(v) => onOpenChange(v as RegisterKey)} className="flex-1 min-h-0 flex flex-col">
          <TabsList>
            {REGISTERS.map((r) => (
              <TabsTrigger key={r.key} value={r.key}>
                {r.label} <span className="text-muted-foreground text-xs ml-1">{r.count(snapshot)}</span>
              </TabsTrigger>
            ))}
          </TabsList>
          <div className="flex-1 min-h-0 overflow-y-auto pr-2 mt-3">
            {open && <RegistersPanel only={open} snapshot={snapshot} canApprove={canApprove} write={write} onSelectRecord={onSelectRecord} />}
          </div>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}

/** The editor for one table or relationship, or the history of a register entry, as a modal. */
function ElementDialog({ editing, onClose, snap, tables, attributes, readOnly, write, path, describe, trace, onSelectRecord, onDeleted }: {
  editing: Selection;
  onClose: () => void;
  snap: Snapshot;
  tables: Map<string, Snapshot["tables"][number]>;
  attributes: Map<string, Snapshot["attributes"][number]>;
  readOnly: boolean;
  write: ReturnType<typeof useWrite>["write"];
  path: { tenant_id: string; project_id: string };
  describe: (id: string) => string | undefined;
  trace: ReturnType<typeof buildTrace> | null;
  onSelectRecord: (id: string) => void;
  onDeleted: () => void;
}) {
  const table = editing?.kind === "table" ? tables.get(editing.id) : undefined;
  const rel = editing?.kind === "relationship" ? snap.relationships.find((r) => r.id === editing.id) : undefined;
  const tableAttrs = table ? snap.attributes.filter((a) => a.data.table_id === table.id) : [];
  const record = editing?.kind === "record" ? editing.id : undefined;
  const title = table ? table.data.name : rel ? describe(rel.id) : "History";
  const kind = table ? TABLE_KINDS.find((k) => k.value === table.data.kind)?.label : rel ? "Relationship" : undefined;
  const open = !!(table || rel || record);
  const [showTrace, setShowTrace] = usePersistentFlag("kleandbm.editorTrace", true);

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className={record ? "sm:max-w-xl max-h-[85vh] overflow-y-auto"
        : `${showTrace ? "sm:max-w-[min(1400px,94vw)]" : "sm:max-w-[min(1000px,94vw)]"} h-[88vh] flex flex-col`}>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 font-mono pr-8">
            {title}
            {kind && <Badge variant="secondary" className="font-sans">{kind}</Badge>}
            {!record && (
              <Button variant="ghost" size="sm" className="ml-auto h-7 font-sans text-xs text-muted-foreground"
                onClick={() => setShowTrace(!showTrace)}>
                {showTrace ? <PanelRightClose className="size-4" /> : <PanelRightOpen className="size-4" />}
                {showTrace ? "Hide requirements & history" : "Requirements & history"}
              </Button>
            )}
          </DialogTitle>
          <DialogDescription className="sr-only">Edit this element and see where it's traced.</DialogDescription>
        </DialogHeader>
        {record && <RecordActivity path={path} recordId={record} describe={describe} />}
        {(table || rel) && (
          <div className={`flex-1 min-h-0 grid gap-6 overflow-hidden ${showTrace ? "md:grid-cols-[minmax(0,1fr)_380px]" : ""}`}>
            <div className="min-h-0 overflow-y-auto pr-2">
              {table && (
                <TableInspector table={table} attributes={tableAttrs} readOnly={readOnly} write={write} onDeleted={onDeleted} />
              )}
              {rel && (
                <RelationshipInspector relationship={rel} tables={tables} attributes={attributes} readOnly={readOnly}
                  write={write} onDeleted={onDeleted} />
              )}
            </div>
            {showTrace && <div className="min-h-0 overflow-y-auto space-y-6 md:border-l md:pl-6">
              {trace && (
                <ElementTrace trace={trace} onSelect={onSelectRecord}
                  ids={table ? [table.id, ...tableAttrs.map((a) => a.id)] : [rel!.id]} />
              )}
              <RecordActivity path={path} recordId={(table ?? rel)!.id} describe={describe} />
            </div>}
          </div>
        )}
      </DialogContent>
    </Dialog>
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
