import { useCallback, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";

type Request = {
  title: string;
  description?: string;
  confirmLabel?: string;
  destructive?: boolean;
  /** When set, the dialog asks for optional text and resolves with it. */
  inputLabel?: string;
};

/**
 * In-app replacement for window.confirm / window.prompt. Native dialogs block the page,
 * look out of place, and can't be driven by browser automation.
 *
 * Usage: const [ask, dialog] = useConfirm(); ... if (await ask({...})) ...; render {dialog}.
 * Resolves to null when cancelled, otherwise to the entered text ("" if there's no input).
 */
export function useConfirm(): [(r: Request) => Promise<string | null>, React.ReactNode] {
  const [request, setRequest] = useState<Request | null>(null);
  const [text, setText] = useState("");
  const resolver = useRef<((v: string | null) => void) | null>(null);

  const ask = useCallback((r: Request) => {
    setText("");
    setRequest(r);
    return new Promise<string | null>((resolve) => {
      resolver.current = resolve;
    });
  }, []);

  const finish = (value: string | null) => {
    resolver.current?.(value);
    resolver.current = null;
    setRequest(null);
  };

  const dialog = (
    <Dialog open={request !== null} onOpenChange={(open) => !open && finish(null)}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>{request?.title}</DialogTitle>
          {request?.description && <DialogDescription>{request.description}</DialogDescription>}
        </DialogHeader>
        {request?.inputLabel && (
          <Textarea rows={3} autoFocus placeholder={request.inputLabel} value={text} onChange={(e) => setText(e.target.value)} />
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={() => finish(null)}>Cancel</Button>
          <Button variant={request?.destructive ? "destructive" : "default"} onClick={() => finish(text)}>
            {request?.confirmLabel ?? "Confirm"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );

  return [ask, dialog];
}
