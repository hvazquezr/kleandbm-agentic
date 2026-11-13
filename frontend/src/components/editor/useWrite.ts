import { toast } from "sonner";
import { ApiError, type Operation } from "@/api/client";
import { useApplyChangeset, type ProjectPath } from "@/api/hooks";

/**
 * Applies a changeset and reports the outcome. A stale-update conflict means someone else
 * changed the record after we loaded it; the snapshot refetches and the user retries on
 * fresh data, so nothing is silently overwritten.
 */
export function useWrite(path: ProjectPath, modelId: string) {
  const mutation = useApplyChangeset(path, modelId);

  const write = async (operations: Operation[], options: { reason?: string; success?: string } = {}) => {
    try {
      const result = await mutation.mutateAsync({ operations, reason: options.reason });
      if (options.success) toast.success(options.success);
      return result;
    } catch (err) {
      if (err instanceof ApiError && err.isStale) {
        toast.warning("Someone else changed this since you loaded it. The latest version is shown; please redo your edit.");
      } else {
        toast.error(err instanceof Error ? err.message : "Save failed");
      }
      return null;
    }
  };

  return { write, pending: mutation.isPending };
}
