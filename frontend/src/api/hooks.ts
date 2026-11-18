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

/** Every lifecycle transition and who made it, newest first. Refreshed with the versions. */
export function useStateHistory(path: ProjectPath, modelId: string) {
  return useQuery({
    queryKey: [...keys.versions(path, modelId), "state"],
    queryFn: () =>
      unwrap(
        api.GET("/tenants/{tenant_id}/projects/{project_id}/models/{model_id}/state/history", {
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

/** Definitions the model depends on; accepting it needs each confirmed or excepted. Keyed
 * under the snapshot so every changeset refreshes it. */
export function useDefinitionDependencies(path: ProjectPath, modelId: string) {
  return useQuery({
    queryKey: [...keys.snapshot(path, modelId), "definition-dependencies"],
    queryFn: () =>
      unwrap(
        api.GET("/tenants/{tenant_id}/projects/{project_id}/models/{model_id}/definition-dependencies", {
          params: { path: { ...path, model_id: modelId } },
        }),
      ),
  });
}

export function useSetState(path: ProjectPath, modelId: string) {
  const invalidate = useInvalidateProjectData(path);
  return useMutation({
    mutationFn: (body: { state: ModelState; note?: string; exceptions?: string[] }) =>
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

// --- Conversations, documents, sources ----------------------------------------------------

export function useConversations(path: ProjectPath, modelId: string | undefined) {
  return useQuery({
    queryKey: ["conversations", path.project_id, modelId ?? "project"],
    queryFn: () =>
      unwrap(
        api.GET("/tenants/{tenant_id}/projects/{project_id}/conversations", {
          params: { path, query: modelId ? { model_id: modelId } : {} },
        }),
      ),
  });
}

export type AgentName = "business_requirements" | "architect";

export function useCreateConversation(path: ProjectPath) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { model_id?: string; title?: string; agent?: AgentName }) =>
      unwrap(api.POST("/tenants/{tenant_id}/projects/{project_id}/conversations", { params: { path }, body: { agent: "business_requirements", ...body } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["conversations", path.project_id] }),
  });
}

export function useMessages(path: ProjectPath, conversationId: string | undefined) {
  return useQuery({
    queryKey: ["messages", path.project_id, conversationId ?? ""],
    enabled: !!conversationId,
    queryFn: () =>
      unwrap(
        api.GET("/tenants/{tenant_id}/projects/{project_id}/conversations/{conversation_id}/messages", {
          params: { path: { ...path, conversation_id: conversationId! } },
        }),
      ),
  });
}

export function useDocuments(path: ProjectPath) {
  return useQuery({
    queryKey: ["documents", path.project_id],
    queryFn: () => unwrap(api.GET("/tenants/{tenant_id}/projects/{project_id}/documents", { params: { path } })),
  });
}

export function useSources(path: ProjectPath) {
  return useQuery({
    queryKey: ["sources", path.project_id],
    queryFn: () => unwrap(api.GET("/tenants/{tenant_id}/projects/{project_id}/sources", { params: { path } })),
  });
}

export function useCreateSource(path: ProjectPath) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; host: string; port: number; database: string; username: string; password: string }) =>
      unwrap(api.POST("/tenants/{tenant_id}/projects/{project_id}/sources", { params: { path }, body })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sources", path.project_id] }),
  });
}

export function useCallSourceTool(path: ProjectPath) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: { connectionId: string; tool: string; arguments?: Record<string, unknown>; purpose?: string }) =>
      unwrap(
        api.POST("/tenants/{tenant_id}/projects/{project_id}/sources/{connection_id}/tools/{tool}", {
          params: { path: { ...path, connection_id: args.connectionId, tool: args.tool } },
          body: { arguments: args.arguments ?? {}, purpose: args.purpose },
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sources", path.project_id] }),
  });
}

/** After an agent turn, everything it may have written needs refetching. */
export function useInvalidateAfterAgent(path: ProjectPath) {
  const qc = useQueryClient();
  return () =>
    Promise.all(
      ["snapshot", "messages", "documents", "history", "impact", "versions", "candidates", "candidate", "content-flags",
        "assessments"].map((k) =>
        qc.invalidateQueries({ queryKey: [k, path.project_id] }),
      ),
    );
}

export function useUpdateSource(path: ProjectPath) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: { id: string; body: { host?: string; port?: number; database?: string; username?: string; password?: string } }) =>
      unwrap(api.PATCH("/tenants/{tenant_id}/projects/{project_id}/sources/{connection_id}", {
        params: { path: { ...path, connection_id: args.id } }, body: args.body,
      })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sources", path.project_id] }),
  });
}

export function useSourceAction(path: ProjectPath, action: "test" | "disable") {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      action === "test"
        ? unwrap(api.POST("/tenants/{tenant_id}/projects/{project_id}/sources/{connection_id}/test", { params: { path: { ...path, connection_id: id } } }))
        : unwrap(api.POST("/tenants/{tenant_id}/projects/{project_id}/sources/{connection_id}/disable", { params: { path: { ...path, connection_id: id } } })),
    onSettled: () => qc.invalidateQueries({ queryKey: ["sources", path.project_id] }),
  });
}

export function useModelSources(path: ProjectPath, modelId: string) {
  return useQuery({
    queryKey: ["model-sources", path.project_id, modelId],
    queryFn: () => unwrap(api.GET("/tenants/{tenant_id}/projects/{project_id}/models/{model_id}/sources", {
      params: { path: { ...path, model_id: modelId } },
    })),
  });
}

export function useLinkModelSource(path: ProjectPath, modelId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: { connectionId: string; linked: boolean }) =>
      args.linked
        ? unwrap(api.PUT("/tenants/{tenant_id}/projects/{project_id}/models/{model_id}/sources/{connection_id}", {
            params: { path: { ...path, model_id: modelId, connection_id: args.connectionId } } }))
        : unwrap(api.DELETE("/tenants/{tenant_id}/projects/{project_id}/models/{model_id}/sources/{connection_id}", {
            params: { path: { ...path, model_id: modelId, connection_id: args.connectionId } } })),
    onSettled: () => qc.invalidateQueries({ queryKey: ["model-sources", path.project_id, modelId] }),
  });
}

// --- Design candidates (the Architect's proposals and the Validator's review) -------------

export function useCandidates(path: ProjectPath, modelId: string, enabled = true) {
  return useQuery({
    queryKey: ["candidates", path.project_id, modelId],
    enabled: enabled && !!modelId,
    queryFn: () => unwrap(api.GET("/tenants/{tenant_id}/projects/{project_id}/models/{model_id}/candidates", {
      params: { path: { ...path, model_id: modelId } },
    })),
  });
}

export function useCandidate(path: ProjectPath, candidateId: string | undefined) {
  return useQuery({
    queryKey: ["candidate", path.project_id, candidateId ?? ""],
    enabled: !!candidateId,
    queryFn: () => unwrap(api.GET("/tenants/{tenant_id}/projects/{project_id}/candidates/{candidate_id}", {
      params: { path: { ...path, candidate_id: candidateId! } },
    })),
  });
}

/** Apply (one changeset; refused with 409 if the model changed since) or discard a proposal. */
export function useCandidateAction(path: ProjectPath, action: "apply" | "discard") {
  const qc = useQueryClient();
  const invalidate = useInvalidateProjectData(path);
  return useMutation({
    mutationFn: (candidateId: string) =>
      action === "apply"
        ? unwrap(api.POST("/tenants/{tenant_id}/projects/{project_id}/candidates/{candidate_id}/apply", {
            params: { path: { ...path, candidate_id: candidateId } } }))
        : unwrap(api.POST("/tenants/{tenant_id}/projects/{project_id}/candidates/{candidate_id}/discard", {
            params: { path: { ...path, candidate_id: candidateId } } })),
    onSettled: () => Promise.all([
      invalidate(),
      qc.invalidateQueries({ queryKey: ["candidates", path.project_id] }),
      qc.invalidateQueries({ queryKey: ["candidate", path.project_id] }),
    ]),
  });
}

/** The candidate's decision-point searches (including undecided ones carried from earlier designs). */
export function useDecisionPoints(path: ProjectPath, candidateId: string) {
  return useQuery({
    queryKey: ["decision-points", path.project_id, candidateId],
    queryFn: () => unwrap(api.GET("/tenants/{tenant_id}/projects/{project_id}/candidates/{candidate_id}/decision-points", {
      params: { path: { ...path, candidate_id: candidateId } },
    })),
  });
}

/** A person accepts or rejects the design left by a search that ended without a selection. */
export function useResolveDecisionPoint(path: ProjectPath) {
  const qc = useQueryClient();
  const invalidate = useInvalidateProjectData(path);
  return useMutation({
    mutationFn: (args: { id: string; outcome: "accepted" | "rejected"; note?: string }) =>
      unwrap(api.POST("/tenants/{tenant_id}/projects/{project_id}/decision-points/{decision_point_id}/resolve", {
        params: { path: { ...path, decision_point_id: args.id } },
        body: { outcome: args.outcome, note: args.note || null },
      })),
    onSettled: () => Promise.all([
      invalidate(),
      qc.invalidateQueries({ queryKey: ["decision-points", path.project_id] }),
      qc.invalidateQueries({ queryKey: ["candidate", path.project_id] }),
    ]),
  });
}

// --- Runs: transcripts, monitor assessments and flagged content ----------------------------

/** A run's summary (agent, status, usage, child run ids). */
export function useRun(path: ProjectPath, runId: string | undefined) {
  return useQuery({
    queryKey: ["run", path.project_id, runId ?? ""],
    enabled: !!runId,
    queryFn: () => unwrap(api.GET("/tenants/{tenant_id}/projects/{project_id}/agent-runs/{run_id}", {
      params: { path: { ...path, run_id: runId! } },
    })),
  });
}

/** The run's transcript and those of the runs it started. Needs edit access. */
export function useRunMessages(path: ProjectPath, runId: string | undefined) {
  return useQuery({
    queryKey: ["run-messages", path.project_id, runId ?? ""],
    enabled: !!runId,
    queryFn: () => unwrap(api.GET("/tenants/{tenant_id}/projects/{project_id}/agent-runs/{run_id}/messages", {
      params: { path: { ...path, run_id: runId! } },
    })),
  });
}

const pending = (rows: { status: string }[] | undefined) => rows?.some((a) => a.status === "pending");

/** The monitor's assessments of one run, newest first; polls while the monitor is reading it. */
export function useRunAssessments(path: ProjectPath, runId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: ["assessments", path.project_id, "run", runId ?? ""],
    enabled: !!runId && enabled,
    refetchInterval: (q) => (pending(q.state.data) ? 5000 : false),
    queryFn: () => unwrap(api.GET("/tenants/{tenant_id}/projects/{project_id}/agent-runs/{run_id}/assessments", {
      params: { path: { ...path, run_id: runId! } },
    })),
  });
}

/** The latest assessment of each run in a conversation. Polls while one is pending, or briefly
 *  after a run (`waitFor`) whose assessment hasn't appeared yet. */
export function useConversationAssessments(path: ProjectPath, conversationId: string | undefined, enabled = true,
                                           waitFor?: string) {
  return useQuery({
    queryKey: ["assessments", path.project_id, "conversation", conversationId ?? ""],
    enabled: !!conversationId && enabled,
    refetchInterval: (q) =>
      pending(q.state.data) || (waitFor && !q.state.data?.some((a) => a.run_id === waitFor)) ? 5000 : false,
    queryFn: () => unwrap(api.GET("/tenants/{tenant_id}/projects/{project_id}/conversations/{conversation_id}/assessments", {
      params: { path: { ...path, conversation_id: conversationId! } },
    })),
  });
}

/** A person's verdict on an assessment. */
export function useReviewAssessment(path: ProjectPath) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: { runId: string; assessmentId: string; verdict: "concern_confirmed" | "false_alarm"; note?: string }) =>
      unwrap(api.POST("/tenants/{tenant_id}/projects/{project_id}/agent-runs/{run_id}/assessments/{assessment_id}/reviews", {
        params: { path: { ...path, run_id: args.runId, assessment_id: args.assessmentId } },
        body: { verdict: args.verdict, note: args.note || null },
      })),
    onSettled: () => qc.invalidateQueries({ queryKey: ["assessments", path.project_id] }),
  });
}

/** Untrusted content agents flagged for trying to instruct them (open ones unless asked). */
export function useContentFlags(path: ProjectPath, includeReviewed = false) {
  return useQuery({
    queryKey: ["content-flags", path.project_id, includeReviewed],
    queryFn: () => unwrap(api.GET("/tenants/{tenant_id}/projects/{project_id}/content-flags", {
      params: { path, query: { include_reviewed: includeReviewed } },
    })),
  });
}

export function useReviewContentFlag(path: ProjectPath) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: { id: string; note?: string }) =>
      unwrap(api.POST("/tenants/{tenant_id}/projects/{project_id}/content-flags/{flag_id}/review", {
        params: { path: { ...path, flag_id: args.id } }, body: { note: args.note || null },
      })),
    onSettled: () => qc.invalidateQueries({ queryKey: ["content-flags", path.project_id] }),
  });
}
