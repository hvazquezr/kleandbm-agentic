import { useAuth0 } from "@auth0/auth0-react";
import { ChevronDown, ChevronRight, House, LogOut, PanelLeft, PanelLeftClose, Search, X } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "wouter";
import type { TableRecord } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/**
 * Floating pieces that sit on the canvas instead of around it: the logo pill that toggles the
 * tables panel, the tables panel itself, toolbar buttons and the right-hand drawer.
 */

export function Pill({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <div className={cn("flex items-center gap-1 rounded-lg border bg-card/70 backdrop-blur-xl px-2 py-1.5 shadow-lg", className)}>
      {children}
    </div>
  );
}

export function LogoPill({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return (
    <Pill className="gap-2 pr-3">
      <ToolButton label={open ? "Hide tables" : "Show tables"} onClick={onToggle}>
        {open ? <PanelLeftClose className="size-4" /> : <PanelLeft className="size-4" />}
      </ToolButton>
      <img src="/kleandbm-logo.svg" alt="kleandbm" className="h-5" />
    </Pill>
  );
}

export function ToolButton({ label, onClick, active, badge, children }: {
  label: string;
  onClick: () => void;
  active?: boolean;
  /** A small count in the corner, e.g. open blocking questions. */
  badge?: number;
  children: React.ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="ghost" size="sm" aria-label={label} aria-pressed={active} onClick={onClick}
          className={cn("relative h-8 px-2", active && "bg-accent text-foreground")}>
          {children}
          {!!badge && (
            <span className="absolute -top-1 -right-1 min-w-4 h-4 px-1 rounded-full bg-amber-500 text-[10px] leading-4 text-black font-medium">
              {badge}
            </span>
          )}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">{label}</TooltipContent>
    </Tooltip>
  );
}

const KIND_ORDER = [
  ["fact", "Facts"],
  ["dimension", "Dimensions"],
  ["bridge", "Bridges"],
] as const;

const KIND_BADGE: Record<string, string> = {
  fact: "bg-purple-500/20 text-purple-300",
  dimension: "bg-blue-500/20 text-blue-300",
  bridge: "bg-amber-500/20 text-amber-300",
};

/** Left panel: the way home, the model's tables grouped by kind, and the signed-in user. */
export function TablesPanel({ projectHref, projectName, tables, attributeCounts, selectedId, onSelect, onOpen }: {
  projectHref: string;
  projectName?: string;
  tables: TableRecord[];
  attributeCounts: Map<string, number>;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onOpen: (id: string) => void;
}) {
  const { user, logout } = useAuth0();
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const shown = tables
      .filter((t) => !q || t.data.name.toLowerCase().includes(q))
      .sort((a, b) => a.data.name.localeCompare(b.data.name));
    return KIND_ORDER.map(([kind, label]) => ({ kind, label, items: shown.filter((t) => t.data.kind === kind) }))
      .filter((g) => g.items.length > 0);
  }, [tables, query]);
  const toggle = (kind: string) =>
    setCollapsed((c) => { const n = new Set(c); if (n.has(kind)) n.delete(kind); else n.add(kind); return n; });

  return (
    <aside className="w-64 shrink-0 border-r bg-card/40 flex flex-col min-h-0">
      <div className="px-3 pt-3 pb-2 space-y-0.5">
        <Link href="/" className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-accent">
          <House className="size-4" /> Home
        </Link>
        {projectName && (
          <Link href={projectHref} className="block truncate rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-foreground">
            {projectName}
          </Link>
        )}
      </div>
      <div className="px-3 pb-2">
        <div className="flex items-baseline justify-between px-2">
          <span className="text-sm font-semibold">Tables</span>
          <span className="text-xs text-muted-foreground">{tables.length}</span>
        </div>
        <div className="relative mt-2">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 size-3.5 text-muted-foreground" />
          <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search tables" className="h-7 pl-7 text-xs" />
        </div>
      </div>
      <nav className="flex-1 min-h-0 overflow-y-auto px-3 pb-3 text-sm" aria-label="Tables">
        {groups.length === 0 && (
          <p className="px-2 py-1 text-xs text-muted-foreground">{tables.length ? "No table matches." : "No tables yet."}</p>
        )}
        {groups.map((g) => (
          <div key={g.kind} className="mt-1">
            <button type="button" onClick={() => toggle(g.kind)}
              className="flex w-full items-center gap-1 px-1 py-1 text-xs text-muted-foreground hover:text-foreground">
              {collapsed.has(g.kind) ? <ChevronRight className="size-3.5" /> : <ChevronDown className="size-3.5" />}
              {g.label} <span className="ml-auto">{g.items.length}</span>
            </button>
            {!collapsed.has(g.kind) && g.items.map((t) => (
              <button key={t.id} type="button"
                onClick={() => onSelect(t.id)} onDoubleClick={() => onOpen(t.id)}
                title="Click to find it on the canvas, double-click to edit"
                className={cn("flex w-full items-center gap-2 rounded-md pl-6 pr-2 py-1 text-left hover:bg-accent",
                  selectedId === t.id && "bg-accent")}>
                <span className="truncate font-mono text-xs">{t.data.name}</span>
                <span className={cn("ml-auto shrink-0 rounded px-1 text-[10px]", KIND_BADGE[t.data.kind])}>
                  {attributeCounts.get(t.id) ?? 0}
                </span>
              </button>
            ))}
          </div>
        ))}
      </nav>
      <div className="border-t px-3 py-2 flex items-center gap-2">
        <div className="size-7 shrink-0 rounded-full bg-primary/20 text-primary flex items-center justify-center text-xs font-medium">
          {(user?.name ?? user?.email ?? "?").slice(0, 1).toUpperCase()}
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-medium">{user?.name ?? user?.email}</div>
          {user?.name && user?.email && user.name !== user.email && (
            <div className="truncate text-[11px] text-muted-foreground">{user.email}</div>
          )}
        </div>
        <ToolButton label="Sign out" onClick={() => logout({ logoutParams: { returnTo: window.location.origin } })}>
          <LogOut className="size-4" />
        </ToolButton>
      </div>
    </aside>
  );
}

/** The right-hand drawer. Only one is open at a time; the editor decides which. */
export function Drawer({ title, width, onClose, hidden, children, actions }: {
  title: React.ReactNode;
  width: number;
  onClose: () => void;
  hidden?: boolean;
  actions?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <aside style={{ width }} className={cn("shrink-0 border-l bg-card/40 flex flex-col min-h-0", hidden && "hidden")}>
      <div className="flex items-center gap-2 px-4 py-2.5 border-b">
        <h2 className="text-sm font-semibold flex items-center gap-2">{title}</h2>
        <div className="ml-auto flex items-center gap-1">
          {actions}
          <ToolButton label="Close" onClick={onClose}><X className="size-4" /></ToolButton>
        </div>
      </div>
      {children}
    </aside>
  );
}

/** Read and write a per-viewer UI preference. Storage can be unavailable; then it isn't kept. */
export function usePersistentFlag(key: string, initial: boolean): [boolean, (v: boolean) => void] {
  const [value, setValue] = useState(() => {
    try {
      const v = window.localStorage.getItem(key);
      return v === null ? initial : v === "1";
    } catch {
      return initial;
    }
  });
  const set = (v: boolean) => {
    setValue(v);
    try { window.localStorage.setItem(key, v ? "1" : "0"); } catch { /* not kept */ }
  };
  return [value, set];
}
