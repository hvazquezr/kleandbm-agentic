import { Save, Trash2 } from "lucide-react";
import type { AttributeRecord, Operation, RelationshipRecord, Schemas, TableRecord } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { CARDINALITIES } from "@/lib/labels";
import { EnumSelect, Field } from "./fields";
import { useConfirm } from "./useConfirm";
import { DraftNoticeBanner, useDraft } from "./useDraft";

type Write = (ops: Operation[], o?: { reason?: string; success?: string }) => Promise<unknown>;
type RelationshipData = Schemas["RelationshipData"];
const LABELS: Partial<Record<keyof RelationshipData, string>> = { role_name: "Role name" };

export function RelationshipInspector({
  relationship,
  tables,
  attributes,
  readOnly,
  write,
  onDeleted,
}: {
  relationship: RelationshipRecord;
  tables: Map<string, TableRecord>;
  attributes: Map<string, AttributeRecord>;
  readOnly: boolean;
  write: Write;
  onDeleted: () => void;
}) {
  const d = relationship.data;
  const [ask, confirmDialog] = useConfirm();
  const draft = useDraft<RelationshipData>(relationship);
  const { form, setForm, patch, dirty } = draft;
  const cardinality = form.cardinality ?? "many_to_one";

  const end = (tableId: string, attrId: string) =>
    `${tables.get(tableId)?.data.name ?? "?"}.${attributes.get(attrId)?.data.name ?? "?"}`;

  return (
    <div className="space-y-4">
      {confirmDialog}
      <DraftNoticeBanner notice={draft.notice} labels={LABELS} noun="relationship" onUseLatest={draft.takeLatest}
        onDiscard={draft.discard} onDismiss={draft.dismiss} />
      <div className="rounded-md border p-3 text-sm space-y-1">
        <div><span className="text-muted-foreground">From </span>{end(d.from_table_id, d.from_attribute_id)}</div>
        <div><span className="text-muted-foreground">To </span>{end(d.to_table_id, d.to_attribute_id)}</div>
      </div>
      <Field label="Cardinality (from → to)">
        <EnumSelect value={cardinality} options={CARDINALITIES} disabled={readOnly} onChange={(v) => v && setForm({ ...form, cardinality: v })} />
      </Field>
      {cardinality === "many_to_many" && (
        <p className="text-xs text-amber-500">Many-to-many usually calls for a bridge table in a dimensional model.</p>
      )}
      <Field label="Role name (for role-playing dimensions, e.g. Order Date)">
        <Input className="h-8" value={form.role_name ?? ""} disabled={readOnly} onChange={(e) => setForm({ ...form, role_name: e.target.value || null })} />
      </Field>
      <Field label="Description">
        <Textarea rows={2} value={form.description ?? ""} disabled={readOnly} onChange={(e) => setForm({ ...form, description: e.target.value || null })} />
      </Field>
      {!readOnly && (
        <div className="flex gap-2">
          <Button size="sm" disabled={!dirty}
            onClick={() => write([{ op: "update", id: relationship.id, expected_seq: relationship.seq, data: patch }], { success: "Relationship saved" })}>
            <Save className="size-4" /> Save
          </Button>
          <Button size="sm" variant="ghost" className="ml-auto text-destructive"
            onClick={async () => {
              const ok = await ask({
                title: "Delete this relationship?",
                description: "The foreign key attribute is kept; delete it separately if it's no longer needed.",
                confirmLabel: "Delete relationship",
                destructive: true,
              });
              if (ok === null) return;
              if (await write([{ op: "delete", id: relationship.id, expected_seq: relationship.seq }], { success: "Relationship deleted" })) onDeleted();
            }}>
            <Trash2 className="size-4" /> Delete
          </Button>
        </div>
      )}
    </div>
  );
}
