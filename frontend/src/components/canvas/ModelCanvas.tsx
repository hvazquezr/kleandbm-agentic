import {
  applyNodeChanges,
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Connection,
  type NodeChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import type { AttributeRecord, RelationshipRecord, TableRecord } from "@/api/client";
import { computeAutoLayout, type Positions } from "./autoLayout";
import RelationshipEdge, { type RelationshipFlowEdge } from "./RelationshipEdge";
import TableNode, { type TableFlowNode } from "./TableNode";

const nodeTypes = { table: TableNode };
const edgeTypes = { relationship: RelationshipEdge };

export type ModelCanvasHandle = {
  /** Positions for every table (saved, or auto-laid-out when none is saved). */
  autoLayout: () => Positions;
  fitView: () => void;
  /** Pan and zoom to one table. */
  focusTable: (id: string) => void;
  /** Flow coordinates for placing a new table in the middle of what's visible. */
  visibleCenter: () => { x: number; y: number };
};

type Props = {
  tables: TableRecord[];
  attributes: AttributeRecord[];
  relationships: RelationshipRecord[];
  savedLayout: Positions;
  readOnly: boolean;
  selectedTableId: string | null;
  selectedRelationshipId: string | null;
  highlightedIds?: Set<string>;
  onSelectTable: (id: string | null) => void;
  /** Double-click: open the table's editor. */
  onOpenTable?: (id: string) => void;
  onSelectRelationship: (id: string) => void;
  onMoveTable: (id: string, x: number, y: number) => void;
  onConnectTables: (fromTableId: string, toTableId: string) => void;
  /** Right-click on empty canvas (with the flow position), a table or a relationship. */
  onContextMenu?: (target: CanvasMenuTarget, screen: { x: number; y: number }) => void;
};

export type CanvasMenuTarget =
  | { kind: "pane"; position: { x: number; y: number } }
  | { kind: "table"; id: string }
  | { kind: "relationship"; id: string };

const Inner = forwardRef<ModelCanvasHandle, Props>(function Inner(props, ref) {
  const {
    tables, attributes, relationships, savedLayout, readOnly,
    selectedTableId, selectedRelationshipId, highlightedIds,
    onSelectTable, onOpenTable, onSelectRelationship, onMoveTable, onConnectTables, onContextMenu,
  } = props;
  const { fitView, screenToFlowPosition, getNode, setCenter, getZoom } = useReactFlow();
  const containerRef = useRef<HTMLDivElement>(null);

  const attributesByTable = useMemo(() => {
    const map = new Map<string, AttributeRecord[]>();
    for (const a of attributes) map.set(a.data.table_id, [...(map.get(a.data.table_id) ?? []), a]);
    return map;
  }, [attributes]);

  const fallbackLayout = useMemo(
    () => computeAutoLayout(tables.map((t) => ({ id: t.id, kind: t.data.kind, attributeCount: attributesByTable.get(t.id)?.length ?? 0 }))),
    [tables, attributesByTable],
  );

  const [nodes, setNodes] = useState<TableFlowNode[]>([]);

  // Rebuild nodes from the snapshot. `savedLayout` includes the editor's unsaved local moves,
  // so it wins. Otherwise keep a node where it already is, then fall back to auto-layout.
  useEffect(() => {
    setNodes((prev) => {
      const prevPos = new Map(prev.map((n) => [n.id, n.position]));
      return tables.map((t) => ({
        id: t.id,
        type: "table" as const,
        position: savedLayout[t.id] ?? prevPos.get(t.id) ?? fallbackLayout[t.id] ?? { x: 0, y: 0 },
        selected: t.id === selectedTableId,
        draggable: !readOnly,
        data: {
          table: t,
          attributes: attributesByTable.get(t.id) ?? [],
          highlighted: highlightedIds?.has(t.id),
          connectable: !readOnly,
        },
      }));
    });
  }, [tables, attributesByTable, savedLayout, fallbackLayout, selectedTableId, readOnly, highlightedIds]);

  const edges = useMemo<RelationshipFlowEdge[]>(() => {
    const groups = new Map<string, RelationshipRecord[]>();
    for (const r of relationships) {
      const key = `${r.data.from_table_id}->${r.data.to_table_id}`;
      groups.set(key, [...(groups.get(key) ?? []), r]);
    }
    return [...groups.entries()].map(([key, rels]) => ({
      id: key,
      type: "relationship" as const,
      source: rels[0].data.from_table_id,
      target: rels[0].data.to_table_id,
      data: {
        relationships: rels, selectedId: selectedRelationshipId, onSelect: onSelectRelationship,
        onContextMenu: onContextMenu && ((id: string, e: React.MouseEvent) =>
          onContextMenu({ kind: "relationship", id }, { x: e.clientX, y: e.clientY })),
      },
    }));
  }, [relationships, selectedRelationshipId, onSelectRelationship, onContextMenu]);

  const onNodesChange = useCallback((changes: NodeChange<TableFlowNode>[]) => {
    // Selection is owned by the editor page, so only apply position and size changes here.
    setNodes((ns) => applyNodeChanges(changes.filter((c) => c.type !== "select"), ns));
  }, []);

  const onConnect = useCallback(
    (c: Connection) => {
      if (c.source && c.target && c.source !== c.target) onConnectTables(c.source, c.target);
    },
    [onConnectTables],
  );

  useImperativeHandle(ref, () => ({
    autoLayout: () => fallbackLayout,
    fitView: () => fitView({ duration: 400, padding: 0.15 }),
    focusTable: (id) => {
      const n = getNode(id);
      if (!n) return;
      const w = n.measured?.width ?? 320, h = n.measured?.height ?? 200;
      void setCenter(n.position.x + w / 2, n.position.y + h / 2, { zoom: Math.max(getZoom(), 0.9), duration: 400 });
    },
    visibleCenter: () => {
      const r = containerRef.current?.getBoundingClientRect();
      if (!r) return { x: 0, y: 0 };
      const c = screenToFlowPosition({ x: r.left + r.width / 2, y: r.top + r.height / 2 });
      // Start with the new node's middle at the centre, then step outward on a grid until it
      // doesn't overlap an existing table.
      const W = 320, H = 140, GAP = 40;
      const boxes = nodes.map((n) => ({
        x: n.position.x, y: n.position.y,
        w: n.measured?.width ?? W, h: n.measured?.height ?? H,
      }));
      const free = (x: number, y: number) =>
        boxes.every((b) => x + W + GAP <= b.x || b.x + b.w + GAP <= x || y + H + GAP <= b.y || b.y + b.h + GAP <= y);
      const start = { x: c.x - W / 2, y: c.y - H / 2 };
      for (let ring = 0; ring <= 6; ring++) {
        for (let dx = -ring; dx <= ring; dx++) {
          for (let dy = -ring; dy <= ring; dy++) {
            if (Math.max(Math.abs(dx), Math.abs(dy)) !== ring) continue; // only the ring's edge
            const x = start.x + dx * (W + GAP), y = start.y + dy * (H + GAP);
            if (free(x, y)) return { x, y };
          }
        }
      }
      return start;
    },
  }), [fallbackLayout, fitView, screenToFlowPosition, getNode, setCenter, getZoom, nodes]);

  return (
    <div ref={containerRef} className="size-full">
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      onNodesChange={onNodesChange}
      onNodeClick={(_, n) => onSelectTable(n.id)}
      onNodeDoubleClick={(_, n) => onOpenTable?.(n.id)}
      zoomOnDoubleClick={false}
      onPaneContextMenu={onContextMenu && ((e) => {
        e.preventDefault();
        const screen = { x: e.clientX, y: e.clientY };
        onContextMenu({ kind: "pane", position: screenToFlowPosition(screen) }, screen);
      })}
      onNodeContextMenu={onContextMenu && ((e, n) => {
        e.preventDefault();
        onContextMenu({ kind: "table", id: n.id }, { x: e.clientX, y: e.clientY });
      })}
      onPaneClick={() => onSelectTable(null)}
      onNodeDragStop={(_, n) => onMoveTable(n.id, n.position.x, n.position.y)}
      onConnect={onConnect}
      nodesConnectable={!readOnly}
      fitView
      fitViewOptions={{ padding: 0.15 }}
      minZoom={0.1}
      proOptions={{ hideAttribution: true }}
      colorMode="dark"
    >
      <Background variant={BackgroundVariant.Dots} gap={16} size={1} />
      <Controls showInteractive={false} position="bottom-left" />
      <MiniMap pannable zoomable position="bottom-right" />
    </ReactFlow>
    </div>
  );
});

const ModelCanvas = forwardRef<ModelCanvasHandle, Props>(function ModelCanvas(props, ref) {
  return (
    <ReactFlowProvider>
      <Inner {...props} ref={ref} />
    </ReactFlowProvider>
  );
});

export default ModelCanvas;
