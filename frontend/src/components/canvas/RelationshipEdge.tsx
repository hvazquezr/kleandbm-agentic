/**
 * Floating edge between two tables, with crow's-foot cardinality markers.
 * Marker drawing and geometry are adapted from the legacy editor. Several relationships
 * between the same pair of tables (role-playing dimensions) share one edge, with their
 * role names stacked in the label.
 */
import { EdgeLabelRenderer, getBezierPath, Position, useInternalNode, type Edge, type EdgeProps } from "@xyflow/react";
import { useState } from "react";
import type { RelationshipRecord } from "@/api/client";
import { getEdgeParams } from "./floatingEdgeUtils";

export type RelationshipEdgeData = {
  relationships: RelationshipRecord[];
  selectedId: string | null;
  onSelect: (relationshipId: string) => void;
  /** Right-click on the line or a role label. */
  onContextMenu?: (relationshipId: string, e: React.MouseEvent) => void;
};

export type RelationshipFlowEdge = Edge<RelationshipEdgeData, "relationship">;

const ROTATION: Record<Position, number> = {
  [Position.Right]: 0,
  [Position.Left]: 180,
  [Position.Bottom]: 90,
  [Position.Top]: -90,
};

function offset(pos: Position, d = 12) {
  return {
    [Position.Right]: { dx: d, dy: 0 },
    [Position.Left]: { dx: -d, dy: 0 },
    [Position.Bottom]: { dx: 0, dy: d },
    [Position.Top]: { dx: 0, dy: -d },
  }[pos];
}

function CrowsFootMany({ color }: { color: string }) {
  const len = 24;
  const spread = 12;
  const barX = -len + 6;
  return (
    <svg width={len + 8} height={spread * 2 + 8} viewBox={`${-len - 4} ${-spread - 4} ${len + 8} ${spread * 2 + 8}`} overflow="visible">
      <line x1={0} y1={0} x2={-len} y2={-spread} stroke={color} strokeWidth={2} strokeLinecap="round" />
      <line x1={0} y1={0} x2={-len} y2={spread} stroke={color} strokeWidth={2} strokeLinecap="round" />
      <line x1={barX} y1={-spread - 2} x2={barX} y2={spread + 2} stroke={color} strokeWidth={2} strokeLinecap="round" />
    </svg>
  );
}

function CrowsFootOne({ color }: { color: string }) {
  const spread = 12;
  return (
    <svg width={22} height={spread * 2 + 8} viewBox={`-22 ${-spread - 4} 22 ${spread * 2 + 8}`} overflow="visible">
      <line x1={-5} y1={-spread} x2={-5} y2={spread} stroke={color} strokeWidth={2} strokeLinecap="round" />
      <line x1={-15} y1={-spread} x2={-15} y2={spread} stroke={color} strokeWidth={2} strokeLinecap="round" />
    </svg>
  );
}

const ENDS: Record<RelationshipRecord["data"]["cardinality"], ["many" | "one", "many" | "one"]> = {
  many_to_one: ["many", "one"],
  one_to_one: ["one", "one"],
  many_to_many: ["many", "many"],
};

function marker(x: number, y: number, rotation: number): React.CSSProperties {
  return {
    position: "absolute",
    transform: `translate(-50%, -50%) translate(${x}px, ${y}px) rotate(${rotation}deg)`,
    pointerEvents: "none",
  };
}

export default function RelationshipEdge({ source, target, data }: EdgeProps<RelationshipFlowEdge>) {
  const [hovered, setHovered] = useState(false);
  const sourceNode = useInternalNode(source);
  const targetNode = useInternalNode(target);
  if (!sourceNode || !targetNode || !data) return null;

  const { relationships, selectedId, onSelect, onContextMenu } = data;
  const menu = (id: string) => (e: React.MouseEvent) => {
    if (!onContextMenu) return;
    e.preventDefault();
    e.stopPropagation();
    onContextMenu(id, e);
  };
  const isSelected = relationships.some((r) => r.id === selectedId);
  const { sx, sy, tx, ty, sourcePos, targetPos } = getEdgeParams(sourceNode, targetNode);
  const [path, labelX, labelY] = getBezierPath({
    sourceX: sx, sourceY: sy, sourcePosition: sourcePos,
    targetX: tx, targetY: ty, targetPosition: targetPos,
  });

  const active = hovered || isSelected;
  const color = active ? "#22d3ee" : "#3b82f6";
  const [fromEnd, toEnd] = ENDS[relationships[0].data.cardinality];
  const so = offset(sourcePos);
  const to = offset(targetPos);
  const roles = relationships.map((r) => r.data.role_name).filter(Boolean) as string[];

  return (
    <>
      <path d={path} fill="none" stroke={active ? "#22d3ee" : "#60a5fa"} strokeWidth={active ? 3 : 2}
        strokeDasharray={hovered ? "8 4" : undefined}
        style={{ animation: hovered ? "edge-dash-flow 0.4s linear infinite" : undefined }} />
      <path d={path} fill="none" stroke="transparent" strokeWidth={20} style={{ cursor: "pointer" }}
        onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}
        onClick={() => onSelect(relationships[0].id)} onContextMenu={menu(relationships[0].id)} />
      <EdgeLabelRenderer>
        <div style={marker(sx + so.dx, sy + so.dy, ROTATION[sourcePos])}>
          {fromEnd === "many" ? <CrowsFootMany color={color} /> : <CrowsFootOne color={color} />}
        </div>
        <div style={marker(tx + to.dx, ty + to.dy, ROTATION[targetPos])}>
          {toEnd === "many" ? <CrowsFootMany color={color} /> : <CrowsFootOne color={color} />}
        </div>
        <div
          className="nodrag nopan absolute"
          style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`, pointerEvents: "all", zIndex: 5 }}
          onMouseEnter={() => setHovered(true)}
          onMouseLeave={() => setHovered(false)}
        >
          {roles.length > 0 ? (
            <span className="flex flex-col items-center gap-0.5 px-2.5 py-1 rounded-md text-xs font-medium bg-card/90 shadow-sm"
              style={{ border: `1.5px solid ${color}` }}>
              {relationships.map((r) => (
                <button key={r.id} type="button" className="hover:underline" onClick={() => onSelect(r.id)} onContextMenu={menu(r.id)}>
                  {r.data.role_name ?? "(no role)"}
                </button>
              ))}
            </span>
          ) : (
            <button type="button" aria-label="Select relationship" onClick={() => onSelect(relationships[0].id)}
              onContextMenu={menu(relationships[0].id)}
              className="block size-5 rounded-full bg-card/90 shadow-sm" style={{ border: `1.5px solid ${color}` }} />
          )}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}
