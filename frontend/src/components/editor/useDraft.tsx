import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";

const same = (a: unknown, b: unknown) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

/** Fields of `next` that differ from `prev`, for a minimal update patch. */
export function diff<T extends object>(prev: T, next: T): Partial<T> {
  const out: Partial<T> = {};
  for (const k of Object.keys(next) as (keyof T)[]) {
    if (!same(prev[k], next[k])) out[k] = next[k];
  }
  return out;
}

export type DraftNotice<T> = { changed: (keyof T)[]; conflicts: (keyof T)[]; latest: T };

/**
 * An edit form over one record revision that survives newer revisions. When someone else (a
 * person or an agent) saves the record while the form has unsaved edits, the form moves onto the
 * latest revision: fields only they changed take their value, fields only you changed keep yours,
 * and fields you both changed keep yours and are listed as conflicts. Saving then sends the latest
 * `seq`, so nothing the other side wrote is overwritten without you seeing it. A form with no edits
 * just follows the latest revision.
 */
export function useDraft<T extends object>(record: { id: string; seq: number; data: T }) {
  const [base, setBase] = useState<T>(record.data); // the revision the edits were made on
  const [form, setForm] = useState<T>(record.data);
  const [notice, setNotice] = useState<DraftNotice<T> | null>(null);
  const lastId = useRef(record.id);

  useEffect(() => {
    const latest = record.data;
    if (lastId.current !== record.id || Object.keys(diff(latest, form)).length === 0) {
      // Another record, or the new revision is what the form already holds (our own save).
      lastId.current = record.id;
      setBase(latest);
      setForm(latest);
      setNotice(null);
      return;
    }
    const mine = diff(base, form);
    if (Object.keys(mine).length === 0) {
      setBase(latest);
      setForm(latest);
      setNotice(null);
      return;
    }
    const theirs = Object.keys(diff(base, latest)) as (keyof T)[];
    const conflicts = theirs.filter((k) => k in mine && !same(mine[k], latest[k]));
    setBase(latest);
    setForm({ ...latest, ...mine });
    setNotice((prev) => ({
      changed: [...new Set([...(prev?.changed ?? []), ...theirs])],
      conflicts: [...new Set([...(prev?.conflicts ?? []).filter((k) => k in mine), ...conflicts])],
      latest,
    }));
  }, [record.id, record.seq]); // eslint-disable-line react-hooks/exhaustive-deps

  const patch = diff(record.data, form);
  return {
    form,
    setForm,
    patch,
    dirty: Object.keys(patch).length > 0,
    notice,
    discard: () => {
      setBase(record.data);
      setForm(record.data);
      setNotice(null);
    },
    takeLatest: (keys: (keyof T)[]) => {
      setForm((f) => {
        const next = { ...f };
        for (const k of keys) next[k] = record.data[k];
        return next;
      });
      setNotice((n) => (n ? { ...n, conflicts: n.conflicts.filter((k) => !keys.includes(k)) } : n));
    },
    dismiss: () => setNotice(null),
  };
}

/** Says a record changed under the form and what to do about it. */
export function DraftNoticeBanner<T extends object>({
  notice,
  labels,
  noun,
  onUseLatest,
  onDiscard,
  onDismiss,
}: {
  notice: DraftNotice<T> | null;
  labels: Partial<Record<keyof T, string>>;
  noun: string;
  onUseLatest: (keys: (keyof T)[]) => void;
  onDiscard: () => void;
  onDismiss: () => void;
}) {
  if (!notice) return null;
  const name = (k: keyof T) => labels[k] ?? String(k).replaceAll("_", " ");
  const show = (v: unknown) => (v === null || v === undefined || v === "" ? "empty" : String(v));
  return (
    <div role="status" className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm space-y-2">
      <p>
        This {noun} was changed by someone else while you were editing
        {notice.changed.length > 0 && <> ({notice.changed.map(name).join(", ")})</>}. Your unsaved edits are kept on
        top of the latest version; review them and save again.
      </p>
      {notice.conflicts.length > 0 && (
        <ul className="space-y-1">
          {notice.conflicts.map((k) => (
            <li key={String(k)} className="flex items-start gap-2">
              <span className="flex-1 min-w-0">
                <span className="font-medium">{name(k)}</span>: you both changed it. Yours is shown; the latest is{" "}
                <span className="italic break-words">“{show(notice.latest[k])}”</span>.
              </span>
              <Button size="sm" variant="outline" className="h-7 shrink-0" onClick={() => onUseLatest([k])}>
                Use latest
              </Button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex gap-2">
        <Button size="sm" variant="ghost" className="h-7" onClick={onDiscard}>Discard my edits</Button>
        {notice.conflicts.length === 0 && (
          <Button size="sm" variant="ghost" className="h-7" onClick={onDismiss}>Dismiss</Button>
        )}
      </div>
    </div>
  );
}
