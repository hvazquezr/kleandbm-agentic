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
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useState } from "react";
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
  onSelectRelationship: (id: string) => void;
  onMoveTable: (id: string, x: number, y: number) => void;
  onConnectTables: (fromTableId: string, toTableId: string) => void;
};

const Inner = forwardRef<ModelCanvasHandle, Props>(function Inner(props, ref) {
  const {
    tables, attributes, relationships, savedLayout, readOnly,
    selectedTableId, selectedRelationshipId, highlightedIds,
    onSelectTable, onSelectRelationship, onMoveTable, onConnectTables,
  } = props;
  const { fitView } = useReactFlow();

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
      data: { relationships: rels, selectedId: selectedRelationshipId, onSelect: onSelectRelationship },
    }));
  }, [relationships, selectedRelationshipId, onSelectRelationship]);

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
  }), [fallbackLayout, fitView]);

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      onNodesChange={onNodesChange}
      onNodeClick={(_, n) => onSelectTable(n.id)}
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
      <Controls showInteractive={false} />
      <MiniMap pannable zoomable />
    </ReactFlow>
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
