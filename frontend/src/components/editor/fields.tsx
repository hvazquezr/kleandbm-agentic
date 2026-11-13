import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { cn } from "@/lib/utils";

export function Field({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("space-y-1.5", className)}>
      <Label className="text-xs text-muted-foreground">{label}</Label>
      {children}
    </div>
  );
}

const NONE = "__none__";

/** A Select over enum options. With `allowNone`, a "None" entry maps to null. */
export function EnumSelect<T extends string>({
  value,
  options,
  onChange,
  disabled,
  allowNone,
  className,
}: {
  value: T | null | undefined;
  options: { value: T; label: string }[];
  onChange: (value: T | null) => void;
  disabled?: boolean;
  allowNone?: boolean;
  className?: string;
}) {
  return (
    <Select
      value={value ?? NONE}
      onValueChange={(v) => onChange(v === NONE ? null : (v as T))}
      disabled={disabled}
    >
      <SelectTrigger className={cn("h-8 w-full", className)}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {allowNone && <SelectItem value={NONE}>None</SelectItem>}
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
