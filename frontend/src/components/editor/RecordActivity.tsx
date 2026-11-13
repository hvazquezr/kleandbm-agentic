import { Bot, User } from "lucide-react";
import { useHistory, useImpact, type ProjectPath } from "@/api/hooks";
import { Badge } from "@/components/ui/badge";
import { humanize } from "@/lib/labels";

/** Change history and impact for one record. */
export function RecordActivity({ path, recordId }: { path: ProjectPath; recordId: string }) {
  const history = useHistory(path, recordId);
  const impact = useImpact(path, recordId);

  return (
    <div className="space-y-5">
      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Affected by a change here</h3>
        {impact.data?.length === 0 && <p className="text-sm text-muted-foreground">Nothing linked.</p>}
        <ul className="space-y-1">
          {impact.data?.map((e) => (
            <li key={e.record.id} className="text-sm flex items-center gap-2">
              <Badge variant="outline" className="font-normal">{e.record.type}</Badge>
              <span className="truncate">{label(e.record)}</span>
              <span className="ml-auto text-xs text-muted-foreground">{humanize(e.via)} · {e.depth} hop{e.depth > 1 ? "s" : ""}</span>
            </li>
          ))}
        </ul>
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-semibold">History</h3>
        <ol className="space-y-2">
          {history.data?.slice().reverse().map((h) => (
            <li key={h.seq} className="text-sm rounded-md border p-2 space-y-0.5">
              <div className="flex items-center gap-2">
                {h.actor_kind === "agent" ? <Bot className="size-3.5" /> : <User className="size-3.5" />}
                <span className="font-mono text-xs">seq {h.seq}</span>
                {h.is_deleted && <Badge variant="destructive">deleted</Badge>}
                <span className="ml-auto text-xs text-muted-foreground">{new Date(h.created_at).toLocaleString()}</span>
              </div>
              {h.reason && <div className="text-muted-foreground text-xs">{h.reason}</div>}
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}

function label(r: { code: string | null; data: Record<string, unknown> }) {
  const d = r.data;
  const text = (d.name ?? d.term ?? d.statement ?? d.summary ?? d.description ?? "") as string;
  return [r.code, text].filter(Boolean).join(" ");
}
