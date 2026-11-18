import { ChevronDown, ChevronRight, Plus, Save, Trash2 } from "lucide-react";
import { useState } from "react";
import type { AttributeRecord, Operation, Schemas, TableRecord } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ADDITIVITY, ATTRIBUTE_ROLES, LOGICAL_TYPES, SCD_TYPES, TABLE_KINDS } from "@/lib/labels";
import { EnumSelect, Field } from "./fields";
import { DraftNoticeBanner, useDraft } from "./useDraft";
import { useConfirm } from "./useConfirm";

type Write = (ops: Operation[], o?: { reason?: string; success?: string }) => Promise<unknown>;
type TableData = Schemas["TableData"];
type AttributeData = Schemas["AttributeData"];

const TABLE_LABELS: Partial<Record<keyof TableData, string>> = { scd_type: "History" };
const ATTRIBUTE_LABELS: Partial<Record<keyof AttributeData, string>> = { logical_type: "Logical type", is_nullable: "Required" };

export function TableInspector({
  table,
  attributes,
  readOnly,
  write,
  onDeleted,
}: {
  table: TableRecord;
  attributes: AttributeRecord[];
  readOnly: boolean;
  write: Write;
  onDeleted: () => void;
}) {
  const draft = useDraft<TableData>(table);
  const { form, setForm, patch, dirty } = draft;
  const [ask, confirmDialog] = useConfirm();
  const sorted = [...attributes].sort((a, b) => a.data.ordinal - b.data.ordinal || a.data.name.localeCompare(b.data.name));

  const save = () =>
    write([{ op: "update", id: table.id, expected_seq: table.seq, data: patch }], { success: "Table saved" });

  const remove = async () => {
    const ok = await ask({
      title: `Delete ${table.data.name}?`,
      description: "Its attributes and relationships are deleted too. The history keeps every earlier version.",
      confirmLabel: "Delete table",
      destructive: true,
    });
    if (ok === null) return;
    if (await write([{ op: "delete", id: table.id, expected_seq: table.seq }], { success: "Table deleted" })) onDeleted();
  };

  return (
    <div className="space-y-5">
      {confirmDialog}
      <section className="space-y-3">
        <DraftNoticeBanner notice={draft.notice} labels={TABLE_LABELS} noun="table" onUseLatest={draft.takeLatest}
          onDiscard={draft.discard} onDismiss={draft.dismiss} />
        <Field label="Name">
          <Input value={form.name} disabled={readOnly} onChange={(e) => setForm({ ...form, name: e.target.value })} className="h-8" />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Kind">
            <EnumSelect value={form.kind} options={TABLE_KINDS} disabled={readOnly} onChange={(v) => v && setForm({ ...form, kind: v })} />
          </Field>
          <Field label="History">
            <EnumSelect value={form.scd_type ?? "none"} options={SCD_TYPES} disabled={readOnly} onChange={(v) => v && setForm({ ...form, scd_type: v })} />
          </Field>
        </div>
        <Field label={form.kind === "fact" ? "Grain (what one row represents)" : "Grain"}>
          <Textarea value={form.grain ?? ""} disabled={readOnly} rows={2}
            placeholder={form.kind === "fact" ? "e.g. One row per order line" : ""}
            onChange={(e) => setForm({ ...form, grain: e.target.value || null })} />
        </Field>
        <Field label="Description">
          <Textarea value={form.description ?? ""} disabled={readOnly} rows={2}
            onChange={(e) => setForm({ ...form, description: e.target.value || null })} />
        </Field>
        {!readOnly && (
          <div className="flex gap-2">
            <Button size="sm" onClick={save} disabled={!dirty || !form.name.trim()}>
              <Save className="size-4" /> Save table
            </Button>
            {dirty && <Button size="sm" variant="ghost" onClick={draft.discard}>Discard</Button>}
            <Button size="sm" variant="ghost" className="ml-auto text-destructive" onClick={remove}>
              <Trash2 className="size-4" /> Delete
            </Button>
          </div>
        )}
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Attributes</h3>
        {sorted.length === 0 && <p className="text-sm text-muted-foreground">No attributes yet.</p>}
        {sorted.map((a) => (
          <AttributeRow key={a.id} attribute={a} readOnly={readOnly} write={write} />
        ))}
        {!readOnly && <NewAttribute tableId={table.id} nextOrdinal={(sorted.at(-1)?.data.ordinal ?? 0) + 1} write={write} />}
      </section>
    </div>
  );
}

function AttributeFields({ value, onChange, readOnly }: { value: AttributeData; onChange: (v: AttributeData) => void; readOnly: boolean }) {
  return (
    <div className="grid grid-cols-2 gap-2">
      <Field label="Name" className="col-span-2">
        <Input value={value.name} disabled={readOnly} className="h-8" onChange={(e) => onChange({ ...value, name: e.target.value })} />
      </Field>
      <Field label="Logical type">
        <EnumSelect value={value.logical_type} options={LOGICAL_TYPES} disabled={readOnly} onChange={(v) => v && onChange({ ...value, logical_type: v })} />
      </Field>
      <Field label="Role">
        <EnumSelect value={value.role ?? "descriptive"} options={ATTRIBUTE_ROLES} disabled={readOnly}
          onChange={(v) => v && onChange({ ...value, role: v, additivity: v === "measure" ? value.additivity : null })} />
      </Field>
      {value.role === "measure" && (
        <Field label="Additivity">
          <EnumSelect value={value.additivity} options={ADDITIVITY} allowNone disabled={readOnly} onChange={(v) => onChange({ ...value, additivity: v })} />
        </Field>
      )}
      <label className="flex items-center gap-2 text-sm col-span-2">
        <input type="checkbox" checked={!(value.is_nullable ?? true)} disabled={readOnly}
          onChange={(e) => onChange({ ...value, is_nullable: !e.target.checked })} />
        Required (not nullable)
      </label>
      <Field label="Description" className="col-span-2">
        <Textarea rows={2} value={value.description ?? ""} disabled={readOnly}
          onChange={(e) => onChange({ ...value, description: e.target.value || null })} />
      </Field>
    </div>
  );
}

function AttributeRow({ attribute, readOnly, write }: { attribute: AttributeRecord; readOnly: boolean; write: Write }) {
  const [open, setOpen] = useState(false);
  const draft = useDraft<AttributeData>(attribute);
  const { form, setForm, patch, dirty } = draft;
  const [ask, confirmDialog] = useConfirm();

  return (
    <div className="rounded-md border">
      {confirmDialog}
      <button type="button" className="w-full flex items-center gap-2 px-3 py-2 text-left text-sm" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
        <span className="font-medium flex-1 truncate">{attribute.data.name}</span>
        {dirty && <span className="text-xs text-amber-600 dark:text-amber-400">unsaved</span>}
        <span className="text-xs text-muted-foreground">{attribute.data.logical_type} · {attribute.data.role?.replaceAll("_", " ")}</span>
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-3">
          <DraftNoticeBanner notice={draft.notice} labels={ATTRIBUTE_LABELS} noun="attribute" onUseLatest={draft.takeLatest}
            onDiscard={draft.discard} onDismiss={draft.dismiss} />
          <AttributeFields value={form} onChange={setForm} readOnly={readOnly} />
          {!readOnly && (
            <div className="flex gap-2">
              <Button size="sm" disabled={!dirty || !form.name.trim()}
                onClick={() => write([{ op: "update", id: attribute.id, expected_seq: attribute.seq, data: patch }], { success: "Attribute saved" })}>
                Save
              </Button>
              <Button size="sm" variant="ghost" className="ml-auto text-destructive"
                onClick={async () => {
                  const ok = await ask({
                    title: `Delete ${attribute.data.name}?`,
                    description: "Relationships that use this attribute are deleted too.",
                    confirmLabel: "Delete attribute",
                    destructive: true,
                  });
                  if (ok !== null) write([{ op: "delete", id: attribute.id, expected_seq: attribute.seq }], { success: "Attribute deleted" });
                }}>
                <Trash2 className="size-4" />
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function NewAttribute({ tableId, nextOrdinal, write }: { tableId: string; nextOrdinal: number; write: Write }) {
  const blank: AttributeData = { table_id: tableId, name: "", logical_type: "text", role: "descriptive", is_nullable: true, additivity: null, description: null, ordinal: nextOrdinal };
  const [form, setForm] = useState<AttributeData | null>(null);

  if (!form) {
    return (
      <Button size="sm" variant="outline" onClick={() => setForm(blank)}>
        <Plus className="size-4" /> Add attribute
      </Button>
    );
  }
  const create = async () => {
    const ok = await write([{ op: "create", type: "attribute", data: { ...form, ordinal: nextOrdinal } }], { success: "Attribute added" });
    if (ok) setForm(null);
  };
  return (
    <div className="rounded-md border p-3 space-y-3">
      <AttributeFields value={form} onChange={setForm} readOnly={false} />
      <div className="flex gap-2">
        <Button size="sm" onClick={create} disabled={!form.name.trim()}>Add</Button>
        <Button size="sm" variant="ghost" onClick={() => setForm(null)}>Cancel</Button>
      </div>
    </div>
  );
}
