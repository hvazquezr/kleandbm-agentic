import { CheckCircle2, Eye, History, Send } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import type { ModelState, Snapshot } from "@/api/client";
import { useCreateVersion, useSetState, useVersions, type ProjectPath } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { STATE_LABELS } from "@/lib/labels";

export function VersionsPanel({
  path,
  modelId,
  snapshot,
  canEdit,
  canApprove,
  viewingSeq,
  onView,
}: {
  path: ProjectPath;
  modelId: string;
  snapshot: Snapshot;
  canEdit: boolean;
  canApprove: boolean;
  viewingSeq: number | undefined;
  onView: (seq: number | undefined) => void;
}) {
  const versions = useVersions(path, modelId);
  const createVersion = useCreateVersion(path, modelId);
  const setState = useSetState(path, modelId);
  const [name, setName] = useState("");
  const state = snapshot.state;

  const transition = async (next: ModelState) => {
    const note = next === "accepted" ? prompt("Acceptance note (optional)") ?? undefined : undefined;
    try {
      await setState.mutateAsync({ state: next, note });
      toast.success(`Model marked ${STATE_LABELS[next]}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not change state");
    }
  };

  return (
    <div className="space-y-6">
      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Lifecycle</h3>
        <div className="text-sm">
          <span className="font-medium">{STATE_LABELS[state.effective_state]}</span>
          {state.changed_since_state && (
            <p className="text-xs text-amber-500 mt-1">
              Was {STATE_LABELS[state.recorded_state]} at seq {state.recorded_at_seq}; changed since, so it's back to Draft.
            </p>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          {canEdit && state.effective_state === "draft" && snapshot.current_seq > 0 && (
            <Button size="sm" variant="outline" onClick={() => transition("ready_for_review")}>
              <Send className="size-4" /> Ready for review
            </Button>
          )}
          {canApprove && state.effective_state !== "accepted" && snapshot.current_seq > 0 && (
            <Button size="sm" onClick={() => transition("accepted")}>
              <CheckCircle2 className="size-4" /> Accept at seq {snapshot.current_seq}
            </Button>
          )}
        </div>
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Named versions</h3>
        {canEdit && (
          <form className="flex gap-2" onSubmit={async (e) => {
            e.preventDefault();
            try {
              await createVersion.mutateAsync({ name: name.trim() });
              setName("");
              toast.success("Version saved");
            } catch (err) {
              toast.error(err instanceof Error ? err.message : "Could not save version");
            }
          }}>
            <Input className="h-8" placeholder={`Name for seq ${snapshot.current_seq}`} value={name} onChange={(e) => setName(e.target.value)} />
            <Button size="sm" type="submit" disabled={!name.trim() || snapshot.current_seq === 0}>Save</Button>
          </form>
        )}
        {versions.data?.length === 0 && <p className="text-sm text-muted-foreground">No named versions yet.</p>}
        <ul className="space-y-1">
          {versions.data?.map((v) => (
            <li key={v.id} className="flex items-center gap-2 text-sm rounded-md border px-2.5 py-1.5">
              <History className="size-4 text-muted-foreground" />
              <span className="font-medium truncate">{v.name}</span>
              <span className="font-mono text-xs text-muted-foreground">seq {v.seq}</span>
              <Button size="sm" variant={viewingSeq === v.seq ? "secondary" : "ghost"} className="ml-auto h-7"
                onClick={() => onView(viewingSeq === v.seq ? undefined : v.seq)}>
                <Eye className="size-4" /> {viewingSeq === v.seq ? "Viewing" : "View"}
              </Button>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
