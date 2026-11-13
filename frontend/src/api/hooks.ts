import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap, type ModelState, type Operation } from "./client";

export type ProjectPath = { tenant_id: string; project_id: string };

const keys = {
  tenants: ["tenants"] as const,
  projects: (t: string) => ["projects", t] as const,
  project: (p: ProjectPath) => ["project", p.tenant_id, p.project_id] as const,
  models: (p: ProjectPath) => ["models", p.project_id] as const,
  snapshot: (p: ProjectPath, m: string, asOf?: number) => ["snapshot", p.project_id, m, asOf ?? "latest"] as const,
  versions: (p: ProjectPath, m: string) => ["versions", p.project_id, m] as const,
  history: (p: ProjectPath, r: string) => ["history", p.project_id, r] as const,
  impact: (p: ProjectPath, r: string) => ["impact", p.project_id, r] as const,
};

// --- Tenants and projects -----------------------------------------------------------------

export function useTenants() {
  return useQuery({ queryKey: keys.tenants, queryFn: () => unwrap(api.GET("/tenants")) });
}

export function useCreateTenant() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => unwrap(api.POST("/tenants", { body: { name } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.tenants }),
  });
}

export function useProjects(tenantId: string | undefined) {
  return useQuery({
    queryKey: keys.projects(tenantId ?? ""),
    enabled: !!tenantId,
    queryFn: () =>
      unwrap(api.GET("/tenants/{tenant_id}/projects", { params: { path: { tenant_id: tenantId! } } })),
  });
}

export function useCreateProject(tenantId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; description?: string }) =>
      unwrap(api.POST("/tenants/{tenant_id}/projects", { params: { path: { tenant_id: tenantId } }, body })),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.projects(tenantId) }),
  });
}

export function useProject(path: ProjectPath) {
  return useQuery({
    queryKey: keys.project(path),
    queryFn: () => unwrap(api.GET("/tenants/{tenant_id}/projects/{project_id}", { params: { path } })),
  });
}

// --- Models -------------------------------------------------------------------------------

export function useModels(path: ProjectPath) {
  return useQuery({
    queryKey: keys.models(path),
    queryFn: () => unwrap(api.GET("/tenants/{tenant_id}/projects/{project_id}/models", { params: { path } })),
  });
}

export function useCreateModel(path: ProjectPath) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; description?: string }) =>
      unwrap(api.POST("/tenants/{tenant_id}/projects/{project_id}/models", { params: { path }, body })),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.models(path) }),
  });
}

export function useSnapshot(path: ProjectPath, modelId: string, asOf?: number) {
  return useQuery({
    queryKey: keys.snapshot(path, modelId, asOf),
    queryFn: () =>
      unwrap(
        api.GET("/tenants/{tenant_id}/projects/{project_id}/models/{model_id}/snapshot", {
          params: { path: { ...path, model_id: modelId }, query: asOf === undefined ? {} : { as_of: asOf } },
        }),
      ),
  });
}

/** Invalidates everything derived from the project's records after a write. */
function useInvalidateProjectData(path: ProjectPath) {
  const qc = useQueryClient();
  return () =>
    Promise.all(
      ["snapshot", "versions", "history", "impact"].map((k) =>
        qc.invalidateQueries({ queryKey: [k, path.project_id] }),
      ),
    );
}

export function useApplyChangeset(path: ProjectPath, modelId: string | null) {
  const invalidate = useInvalidateProjectData(path);
  return useMutation({
    mutationFn: (args: { operations: Operation[]; reason?: string }) =>
      unwrap(
        api.POST("/tenants/{tenant_id}/projects/{project_id}/changesets", {
          params: { path },
          body: { model_id: modelId, operations: args.operations, reason: args.reason },
        }),
      ),
    // Refetch on success and on failure: a stale-update error means our copy is out of date.
    onSettled: invalidate,
  });
}

export function useSetLayout(path: ProjectPath, modelId: string) {
  return useMutation({
    mutationFn: (positions: { record_id: string; x: number; y: number }[]) =>
      unwrap(
        api.PUT("/tenants/{tenant_id}/projects/{project_id}/models/{model_id}/layout", {
          params: { path: { ...path, model_id: modelId } },
          body: { positions },
        }),
      ),
  });
}

// --- Versions, state, history, impact -----------------------------------------------------

export function useVersions(path: ProjectPath, modelId: string) {
  return useQuery({
    queryKey: keys.versions(path, modelId),
    queryFn: () =>
      unwrap(
        api.GET("/tenants/{tenant_id}/projects/{project_id}/models/{model_id}/versions", {
          params: { path: { ...path, model_id: modelId } },
        }),
      ),
  });
}

export function useCreateVersion(path: ProjectPath, modelId: string) {
  const invalidate = useInvalidateProjectData(path);
  return useMutation({
    mutationFn: (body: { name: string; description?: string }) =>
      unwrap(
        api.POST("/tenants/{tenant_id}/projects/{project_id}/models/{model_id}/versions", {
          params: { path: { ...path, model_id: modelId } },
          body,
        }),
      ),
    onSuccess: invalidate,
  });
}

export function useSetState(path: ProjectPath, modelId: string) {
  const invalidate = useInvalidateProjectData(path);
  return useMutation({
    mutationFn: (body: { state: ModelState; note?: string }) =>
      unwrap(
        api.POST("/tenants/{tenant_id}/projects/{project_id}/models/{model_id}/state", {
          params: { path: { ...path, model_id: modelId } },
          body,
        }),
      ),
    onSuccess: invalidate,
  });
}

export function useHistory(path: ProjectPath, recordId: string | null) {
  return useQuery({
    queryKey: keys.history(path, recordId ?? ""),
    enabled: !!recordId,
    queryFn: () =>
      unwrap(
        api.GET("/tenants/{tenant_id}/projects/{project_id}/records/{record_id}/history", {
          params: { path: { ...path, record_id: recordId! } },
        }),
      ),
  });
}

export function useImpact(path: ProjectPath, recordId: string | null) {
  return useQuery({
    queryKey: keys.impact(path, recordId ?? ""),
    enabled: !!recordId,
    queryFn: () =>
      unwrap(
        api.GET("/tenants/{tenant_id}/projects/{project_id}/records/{record_id}/impact", {
          params: { path: { ...path, record_id: recordId! }, query: { depth: 2 } },
        }),
      ),
  });
}
