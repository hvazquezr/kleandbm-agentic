import { Plus, Sparkles, X } from "lucide-react";
import { useState } from "react";
import type { Schemas } from "@/api/client";
import { useTableSuggestion, type ProjectPath } from "@/api/hooks";
import { EnumSelect, Field } from "@/components/editor/fields";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ADDITIVITY, ATTRIBUTE_ROLES, LOGICAL_TYPES, SCD_TYPES, TABLE_KINDS } from "@/lib/labels";

type Table = Schemas["TableData"];
export type NewAttribute = Omit<Schemas["AttributeData"], "table_id" | "ordinal">;

const blankAttribute = (): NewAttribute => ({ name: "", logical_type: "text", role: "descriptive", is_nullable: true, additivity: null, description: null });

/**
 * The New table form. "Describe with AI" fills it in from one model call; nothing is saved until
 * the user clicks Create, so the table and its attributes are the user's own edit.
 */
export function AddTableDialog({ path, modelId, onClose, onCreate }: {
  path: ProjectPath;
  modelId: string;
  onClose: () => void;
  onCreate: (table: Table, attributes: NewAttribute[]) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState<Table["kind"]>("dimension");
  const [grain, setGrain] = useState("");
  const [description, setDescription] = useState("");
  const [scd, setScd] = useState<NonNullable<Table["scd_type"]>>("none");
  const [attributes, setAttributes] = useState<NewAttribute[]>([]);
  const [prompt, setPrompt] = useState("");
  const [filled, setFilled] = useState<{ notes: string | null } | null>(null);
  const [saving, setSaving] = useState(false);
  const suggest = useTableSuggestion(path, modelId);

  const fill = async () => {
    if (!prompt.trim() || suggest.isPending) return;
    try {
      const s = await suggest.mutateAsync(prompt.trim());
      setName(s.table.name);
      setKind(s.table.kind);
      setGrain(s.table.grain ?? "");
      setDescription(s.table.description ?? "");
      setScd(s.table.scd_type ?? "none");
      setAttributes(s.attributes.map((a) => ({ ...blankAttribute(), ...a })));
      setFilled({ notes: s.notes ?? null });
    } catch {
      /* shown from suggest.error */
    }
  };
  const update = (i: number, patch: Partial<NewAttribute>) =>
    setAttributes((list) => list.map((a, j) => (j === i ? { ...a, ...patch } : a)));
  const named = attributes.filter((a) => a.name.trim());
  const create = async () => {
    if (!name.trim() || saving) return;
    setSaving(true);
    try {
      await onCreate(
        { name: name.trim(), kind, grain: kind === "fact" ? grain.trim() || null : null,
          description: description.trim() || null, scd_type: kind === "dimension" ? scd : "none" },
        named.map((a) => ({ ...a, name: a.name.trim(), description: a.description?.trim() || null,
                            additivity: a.role === "measure" ? a.additivity ?? null : null })),
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className={attributes.length ? "sm:max-w-4xl" : "sm:max-w-lg"}>
        <DialogHeader>
          <DialogTitle>New table</DialogTitle>
          <DialogDescription>Fill it in yourself, or describe it and let AI fill it in for you to check.</DialogDescription>
        </DialogHeader>
        <div className="max-h-[70vh] overflow-y-auto space-y-4 pr-1">
          <div className="rounded-md border bg-muted/30 p-3 space-y-2">
            <Field label="Describe with AI">
              <Textarea rows={2} aria-label="Describe the table for AI" value={prompt} onChange={(e) => setPrompt(e.target.value)} disabled={suggest.isPending}
                placeholder="e.g. Create a dim_vendor table with a surrogate key, the vendor's id and name"
                onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); fill(); } }} />
            </Field>
            <div className="flex items-center gap-2">
              <Button type="button" size="sm" variant="secondary" disabled={!prompt.trim() || suggest.isPending} onClick={fill}>
                <Sparkles className="size-4" /> {suggest.isPending ? "Filling in…" : filled ? "Fill in again" : "Fill in"}
              </Button>
              <span className="text-xs text-muted-foreground">Replaces the form below. Nothing is saved until you click Create.</span>
            </div>
            {suggest.error && <p role="alert" className="text-xs text-destructive">{suggest.error.message}</p>}
            {filled && (
              <div className="text-xs text-muted-foreground space-y-1">
                <p>Filled in by AI. Check it before you create it: the table is saved as your edit.</p>
                {filled.notes && <p className="text-foreground/80">AI notes: {filled.notes}</p>}
              </div>
            )}
          </div>

          <form id="add-table" className="space-y-3" onSubmit={(e) => { e.preventDefault(); create(); }}>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Name"><Input autoFocus className="h-8" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. dim_customer" /></Field>
              <Field label="Kind"><EnumSelect value={kind} options={TABLE_KINDS} onChange={(v) => v && setKind(v)} /></Field>
            </div>
            {kind === "fact" && (
              <Field label="Grain"><Input className="h-8" value={grain} onChange={(e) => setGrain(e.target.value)} placeholder="One row per order line" /></Field>
            )}
            {kind === "dimension" && (
              <Field label="History"><EnumSelect value={scd} options={SCD_TYPES} onChange={(v) => v && setScd(v)} /></Field>
            )}
            <Field label="Description"><Input className="h-8" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Optional" /></Field>

            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-xs text-muted-foreground">Attributes{attributes.length ? ` (${attributes.length})` : ""}</span>
                <Button type="button" size="sm" variant="ghost" onClick={() => setAttributes((l) => [...l, blankAttribute()])}>
                  <Plus className="size-4" /> Add attribute
                </Button>
              </div>
              {attributes.length === 0 && <p className="text-xs text-muted-foreground">None yet. You can also add them later in the table editor.</p>}
              {attributes.map((a, i) => (
                <div key={i} className="rounded-md border p-2 space-y-1.5">
                  <div className="grid grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,1.2fr)_auto_auto] gap-1.5 items-center">
                    <Input className="h-8" aria-label="Attribute name" value={a.name} placeholder="name" onChange={(e) => update(i, { name: e.target.value })} />
                    <EnumSelect value={a.logical_type} options={LOGICAL_TYPES} onChange={(v) => v && update(i, { logical_type: v })} />
                    <EnumSelect value={a.role ?? "descriptive"} options={ATTRIBUTE_ROLES} onChange={(v) => v && update(i, { role: v })} />
                    <label className="flex items-center gap-1 text-xs text-muted-foreground whitespace-nowrap">
                      <input type="checkbox" checked={a.is_nullable ?? true} onChange={(e) => update(i, { is_nullable: e.target.checked })} /> Nullable
                    </label>
                    <Button type="button" size="icon" variant="ghost" className="size-8" aria-label={`Remove ${a.name || "attribute"}`}
                      onClick={() => setAttributes((l) => l.filter((_, j) => j !== i))}><X className="size-4" /></Button>
                  </div>
                  <div className={a.role === "measure" ? "grid grid-cols-[minmax(0,1fr)_10rem] gap-1.5" : ""}>
                    <Input className="h-7 text-xs" aria-label="Attribute description" value={a.description ?? ""} placeholder="Description (optional)"
                      onChange={(e) => update(i, { description: e.target.value })} />
                    {a.role === "measure" && (
                      <EnumSelect className="h-7" value={a.additivity ?? null} options={ADDITIVITY} allowNone onChange={(v) => update(i, { additivity: v })} />
                    )}
                  </div>
                </div>
              ))}
            </div>
          </form>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="add-table" disabled={!name.trim() || saving || suggest.isPending}>
            {named.length ? `Create with ${named.length} attribute${named.length === 1 ? "" : "s"}` : "Create"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
