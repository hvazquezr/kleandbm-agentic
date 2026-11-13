import { Plus } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/** A button that expands into a one-field "name" form. */
export function InlineCreate({
  label,
  placeholder,
  pending,
  onCreate,
}: {
  label: string;
  placeholder: string;
  pending?: boolean;
  onCreate: (name: string) => Promise<unknown>;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");

  if (!open) {
    return (
      <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
        <Plus className="size-4" /> {label}
      </Button>
    );
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    try {
      await onCreate(name.trim());
      setName("");
      setOpen(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not create");
    }
  };

  return (
    <form onSubmit={submit} className="flex gap-2">
      <Input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder={placeholder} className="h-8" />
      <Button type="submit" size="sm" disabled={pending || !name.trim()}>Create</Button>
      <Button type="button" size="sm" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
    </form>
  );
}
