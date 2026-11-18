import { Flag, Loader2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import type { ContentFlag } from "@/api/client";
import { useContentFlags, useReviewContentFlag, type ProjectPath } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { humanize } from "@/lib/labels";

/**
 * Untrusted content that an agent found trying to instruct it. The quote is the planted text:
 * it's shown as data, never followed. Flags don't block anything; a person marks them reviewed
 * once they've looked at the document or source.
 */

export function flagPlace(f: ContentFlag): string {
  if (f.target_kind === "document") {
    const loc = f.location ? Object.entries(f.location).map(([k, v]) => `${k} ${Array.isArray(v) ? v.join("–") : String(v)}`).join(", ") : "";
    return `${f.document_title ?? "a document"}${f.version_no ? ` v${f.version_no}` : ""}${loc ? ` · ${loc}` : ""}`;
  }
  return `${f.source_name ?? "a source"} · ${[f.source_table, f.source_column].filter(Boolean).join(".")}`;
}

function FlagItem({ path, flag, canEdit }: { path: ProjectPath; flag: ContentFlag; canEdit: boolean }) {
  const review = useReviewContentFlag(path);
  const [note, setNote] = useState("");
  const [open, setOpen] = useState(false);
  const submit = async () => {
    try {
      await review.mutateAsync({ id: flag.id, note });
      toast.success("Flag marked reviewed");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't mark it reviewed");
    }
  };
  return (
    <li className="rounded-md border border-amber-500/40 bg-amber-500/5 p-2 text-xs space-y-1">
      <p className="font-medium">{flagPlace(flag)}</p>
      <p className="italic break-words text-foreground/90" title="The flagged text, as data">“{flag.quote}”</p>
      <p className="text-muted-foreground">{flag.reason}</p>
      <p className="text-muted-foreground">
        Flagged by the {humanize(flag.agent)} agent · {new Date(flag.created_at).toLocaleString()}
        {flag.reviewed_at && <> · reviewed{flag.reviewed_by_name && ` by ${flag.reviewed_by_name}`}{flag.review_note && `: ${flag.review_note}`}</>}
      </p>
      {canEdit && !flag.reviewed_at && (open ? (
        <div className="flex gap-2 pt-1">
          <Input className="h-7 text-xs" value={note} placeholder="What you did about it (optional)" onChange={(e) => setNote(e.target.value)} />
          <Button size="sm" className="h-7" disabled={review.isPending} onClick={submit}>
            {review.isPending && <Loader2 className="size-3 animate-spin" />} Mark reviewed
          </Button>
        </div>
      ) : (
        <Button size="sm" variant="outline" className="h-6 px-2 text-xs" onClick={() => setOpen(true)}>Mark reviewed</Button>
      ))}
    </li>
  );
}

/** Open flags for the project, or only those on one document or source. Renders nothing when there are none. */
export function FlaggedContent({ path, canEdit, documentId, sourceId, title = "Flagged content" }: {
  path: ProjectPath;
  canEdit: boolean;
  documentId?: string;
  sourceId?: string;
  title?: string;
}) {
  const flags = useContentFlags(path);
  const rows = (flags.data ?? []).filter((f) =>
    (!documentId || f.document_id === documentId) && (!sourceId || f.source_id === sourceId));
  if (rows.length === 0) return null;
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold flex items-center gap-2">
        <Flag className="size-4 text-amber-500" /> {title} <span className="text-muted-foreground font-normal">{rows.length}</span>
      </h3>
      <p className="text-xs text-muted-foreground">Text that tried to instruct an agent. The agents ignored it; check the content and fix it at its source.</p>
      <ul className="space-y-1.5">
        {rows.map((f) => <FlagItem key={f.id} path={path} flag={f} canEdit={canEdit} />)}
      </ul>
    </section>
  );
}

/** Open flag counts per document and per source, for badges in lists. */
export function useFlagCounts(path: ProjectPath) {
  const flags = useContentFlags(path);
  const byDocument = new Map<string, number>();
  const bySource = new Map<string, number>();
  for (const f of flags.data ?? []) {
    if (f.document_id) byDocument.set(f.document_id, (byDocument.get(f.document_id) ?? 0) + 1);
    if (f.source_id) bySource.set(f.source_id, (bySource.get(f.source_id) ?? 0) + 1);
  }
  return { byDocument, bySource };
}
