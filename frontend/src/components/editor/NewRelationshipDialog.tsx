import { ArrowLeftRight } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { AttributeRecord, Operation, Schemas, TableRecord } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { CARDINALITIES } from "@/lib/labels";
import { EnumSelect, Field } from "./fields";

type Write = (ops: Operation[], o?: { reason?: string; success?: string }) => Promise<unknown>;
type Cardinality = NonNullable<Schemas["RelationshipData"]["cardinality"]>;

const NEW_FK = "__new__";

/**
 * Created by dragging from one table to another. The "from" side holds the foreign key
 * (usually the fact). It can reuse an existing attribute or create a new FK attribute
 * in the same changeset as the relationship.
 */
export function NewRelationshipDialog({
  from,
  to,
  attributes,
  write,
  onSwap,
  onClose,
}: {
  from: TableRecord;
  to: TableRecord;
  attributes: AttributeRecord[];
  write: Write;
  onSwap: () => void;
  onClose: () => void;
}) {
  const fromAttrs = useMemo(() => attributes.filter((a) => a.data.table_id === from.id), [attributes, from.id]);
  const toAttrs = useMemo(() => attributes.filter((a) => a.data.table_id === to.id), [attributes, to.id]);
  const defaultKey = toAttrs.find((a) => a.data.role === "surrogate_key") ?? toAttrs.find((a) => a.data.role === "natural_key") ?? toAttrs[0];

  const [toAttrId, setToAttrId] = useState(defaultKey?.id ?? "");
  const [fromAttrId, setFromAttrId] = useState(NEW_FK);
  const [newFkName, setNewFkName] = useState("");
  const [cardinality, setCardinality] = useState<Cardinality>("many_to_one");
  const [roleName, setRoleName] = useState("");

  // Swapping direction re-mounts nothing, so reset the picks for the new pair of tables.
  useEffect(() => {
    setToAttrId(defaultKey?.id ?? "");
    setFromAttrId(NEW_FK);
  }, [from.id, to.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const targetKey = toAttrs.find((a) => a.id === toAttrId);
  useEffect(() => {
    // Suggest an FK name from the role and target key, e.g. "order_date_key".
    const base = targetKey?.data.name ?? "";
    const slug = roleName.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
    setNewFkName(slug && base && !base.startsWith(slug) ? `${slug}_${base}` : base);
  }, [targetKey?.data.name, roleName]);

  const canCreate = !!targetKey && (fromAttrId !== NEW_FK || newFkName.trim().length > 0);

  const create = async () => {
    if (!targetKey) return;
    const ops: Operation[] = [];
    let fromAttribute: string = fromAttrId;
    if (fromAttrId === NEW_FK) {
      ops.push({
        op: "create", type: "attribute", ref: "fk",
        data: {
          table_id: from.id, name: newFkName.trim(), logical_type: targetKey.data.logical_type, role: "foreign_key",
          is_nullable: false, ordinal: Math.max(0, ...fromAttrs.map((a) => a.data.ordinal)) + 1,
        },
      });
      fromAttribute = "$fk";
    }
    ops.push({
      op: "create", type: "relationship",
      data: {
        from_table_id: from.id, from_attribute_id: fromAttribute, to_table_id: to.id, to_attribute_id: targetKey.id,
        cardinality, role_name: roleName.trim() || null,
      },
    });
    if (await write(ops, { success: "Relationship created" })) onClose();
  };

  const attrOptions = (list: AttributeRecord[]) => list.map((a) => ({ value: a.id, label: `${a.data.name} (${a.data.logical_type})` }));

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Relate {from.data.name} → {to.data.name}</DialogTitle>
        </DialogHeader>
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <span>
            The foreign key goes in <span className="font-medium text-foreground">{from.data.name}</span> and
            references <span className="font-medium text-foreground">{to.data.name}</span>.
          </span>
          <Button size="sm" variant="outline" className="ml-auto shrink-0" onClick={onSwap}>
            <ArrowLeftRight className="size-4" /> Swap direction
          </Button>
        </div>
        {toAttrs.length === 0 ? (
          <p className="text-sm text-muted-foreground">{to.data.name} has no attributes yet. Add its key first.</p>
        ) : (
          <div className="space-y-3">
            <Field label={`Key in ${to.data.name}`}>
              <EnumSelect value={toAttrId} options={attrOptions(toAttrs)} onChange={(v) => v && setToAttrId(v)} />
            </Field>
            <Field label={`Foreign key in ${from.data.name}`}>
              <EnumSelect value={fromAttrId} options={[{ value: NEW_FK, label: "Create a new foreign key attribute" }, ...attrOptions(fromAttrs)]}
                onChange={(v) => v && setFromAttrId(v)} />
            </Field>
            {fromAttrId === NEW_FK && (
              <Field label="New attribute name">
                <Input className="h-8" value={newFkName} onChange={(e) => setNewFkName(e.target.value)} />
              </Field>
            )}
            <div className="grid grid-cols-2 gap-3">
              <Field label="Cardinality">
                <EnumSelect value={cardinality} options={CARDINALITIES} onChange={(v) => v && setCardinality(v)} />
              </Field>
              <Field label="Role name (optional)">
                <Input className="h-8" value={roleName} placeholder="e.g. Ship Date" onChange={(e) => setRoleName(e.target.value)} />
              </Field>
            </div>
          </div>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button onClick={create} disabled={!canCreate}>Create relationship</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
