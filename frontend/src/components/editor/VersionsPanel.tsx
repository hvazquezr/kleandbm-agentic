import { CheckCircle2, Eye, History, Send } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import type { ModelState, Snapshot } from "@/api/client";
import {
  useCreateVersion, useDefinitionDependencies, useSetState, useStateHistory, useVersions, type ProjectPath,
} from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { STATE_LABELS } from "@/lib/labels";
import { useConfirm } from "./useConfirm";

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
  const lifecycle = useStateHistory(path, modelId);
  const deps = useDefinitionDependencies(path, modelId);
  const [name, setName] = useState("");
  const [excepted, setExcepted] = useState<Set<string>>(new Set());
  const [ask, confirmDialog] = useConfirm();
  const state = snapshot.state;
  // State changes and new versions always apply to the latest seq, so they're hidden while
  // an older version is on screen. Otherwise you could accept something you aren't looking at.
  const viewingPast = viewingSeq !== undefined;

  // Accepting needs every definition the model depends on confirmed by a person, or named as
  // an exception (accepted as an assumption, with a note). Rejected ones must be fixed first.
  const openDefs = (deps.data ?? []).filter((d) => d.confirmation_status === "unconfirmed");
  const rejectedDefs = (deps.data ?? []).filter((d) => d.confirmation_status === "rejected");
  const exceptions = openDefs.filter((d) => excepted.has(d.id)).map((d) => d.id);
  const acceptBlocked = rejectedDefs.length > 0 || exceptions.length < openDefs.length;

  const transition = async (next: ModelState) => {
    let note: string | undefined;
    if (next === "accepted") {
      const entered = await ask({
        title: `Accept the model at seq ${snapshot.current_seq}?`,
        description: exceptions.length
          ? `You're accepting ${exceptions.length} unconfirmed definition${exceptions.length > 1 ? "s" : ""} as exceptions. Say why; the note is kept with the acceptance. Any later change returns the model to Draft.`
          : "Acceptance is recorded against this version. Any later change returns the model to Draft.",
        inputLabel: exceptions.length ? "Why these definitions can stay unconfirmed (required)" : "Acceptance note (optional)",
        inputRequired: exceptions.length > 0,
        confirmLabel: "Accept",
      });
      if (entered === null) return;
      note = entered.trim() || undefined;
    }
    try {
      await setState.mutateAsync({ state: next, note, exceptions: next === "accepted" ? exceptions : undefined });
      setExcepted(new Set());
      toast.success(`Model marked ${STATE_LABELS[next]}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not change state");
    }
  };

  return (
    <div className="space-y-6">
      {confirmDialog}
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
        {viewingPast && (
          <p className="text-xs text-muted-foreground">You're viewing seq {viewingSeq}. Return to the latest version to change state or save a version.</p>
        )}
        <div className={viewingPast ? "hidden" : "flex flex-wrap gap-2"}>
          {canEdit && state.effective_state === "draft" && snapshot.current_seq > 0 && (
            <Button size="sm" variant="outline" onClick={() => transition("ready_for_review")}>
              <Send className="size-4" /> Ready for review
            </Button>
          )}
          {canApprove && state.effective_state !== "accepted" && snapshot.current_seq > 0 && (
            <Button size="sm" disabled={acceptBlocked} onClick={() => transition("accepted")}>
              <CheckCircle2 className="size-4" /> Accept at seq {snapshot.current_seq}
            </Button>
          )}
        </div>
        {!viewingPast && state.effective_state !== "accepted" && (openDefs.length > 0 || rejectedDefs.length > 0) && (
          <div className="rounded-md border border-amber-500/40 p-2.5 space-y-2">
            <p className="text-xs">
              {rejectedDefs.length > 0
                ? "The model still uses rejected definitions. Revise the model or its requirements before accepting it."
                : canApprove
                  ? "These definitions aren't confirmed yet. Confirm them in Registers, or tick each one to accept it as an exception."
                  : "These definitions need confirming before the model can be accepted."}
            </p>
            <ul className="space-y-1.5">
              {[...rejectedDefs, ...openDefs].map((d) => (
                <li key={d.id} className="text-xs">
                  <label className="flex items-start gap-2">
                    {d.confirmation_status === "unconfirmed" && canApprove && (
                      <input type="checkbox" className="mt-0.5" checked={excepted.has(d.id)}
                        onChange={(e) => setExcepted((prev) => {
                          const nextSet = new Set(prev);
                          if (e.target.checked) nextSet.add(d.id); else nextSet.delete(d.id);
                          return nextSet;
                        })} />
                    )}
                    <span className="min-w-0">
                      <span className="font-medium">{d.code ? `${d.code} ` : ""}{d.term}</span>{" "}
                      <span className={d.confirmation_status === "rejected" ? "text-destructive" : "text-amber-500"}>
                        {d.confirmation_status}
                      </span>
                      <span className="text-muted-foreground"> · {d.provenance.replace("_", " ")} · {d.reasons.join("; ")}</span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      {!!lifecycle.data?.length && (
        <section className="space-y-2">
          <h3 className="text-sm font-semibold">Lifecycle history</h3>
          <ol className="space-y-1.5">
            {lifecycle.data.map((e, i) => (
              <li key={i} className="text-sm rounded-md border px-2.5 py-1.5">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{STATE_LABELS[e.state]}</span>
                  {e.seq !== null && <span className="font-mono text-xs text-muted-foreground">seq {e.seq}</span>}
                  <span className="ml-auto text-xs text-muted-foreground">{new Date(e.at).toLocaleString()}</span>
                </div>
                <div className="text-xs text-muted-foreground">
                  {e.actor_kind === "agent"
                    ? `Agent, on behalf of ${e.by_name ?? "a project member"}`
                    : `By ${e.by_name ?? "a project member (no name on file)"}`}
                </div>
                {e.note && <div className="text-xs mt-0.5">“{e.note}”</div>}
                {!!e.exceptions?.length && (
                  <div className="text-xs mt-0.5 text-amber-500">
                    Accepted with unconfirmed: {e.exceptions.map((x) => String(x.code ?? x.term)).join(", ")}
                  </div>
                )}
              </li>
            ))}
          </ol>
        </section>
      )}

      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Named versions</h3>
        {canEdit && !viewingPast && (
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
