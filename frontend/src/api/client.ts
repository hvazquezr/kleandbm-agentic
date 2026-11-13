/**
 * Typed API client generated from the backend's OpenAPI document (src/api/schema.d.ts).
 * Regenerate with `npm run gen:api` whenever backend routes or schemas change.
 */
import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type Snapshot = Schemas["SnapshotOut"];
export type TableRecord = Snapshot["tables"][number];
export type AttributeRecord = Snapshot["attributes"][number];
export type RelationshipRecord = Snapshot["relationships"][number];
export type Operation = Schemas["ChangesetIn"]["operations"][number];
export type ModelState = Schemas["StateOut"]["effective_state"];

let tokenGetter: (() => Promise<string>) | null = null;

/** Called once by the auth provider so every request carries a fresh access token. */
export function setTokenGetter(getter: () => Promise<string>) {
  tokenGetter = getter;
}

const auth: Middleware = {
  async onRequest({ request }) {
    if (tokenGetter) request.headers.set("Authorization", `Bearer ${await tokenGetter()}`);
    return request;
  },
};

export const api = createClient<paths>({ baseUrl: "/api" });
api.use(auth);

/** Thrown for any non-2xx response so React Query sees a failure. */
export class ApiError extends Error {
  constructor(
    public status: number,
    public body: { error?: string; detail?: unknown; [k: string]: unknown },
  ) {
    super(typeof body?.detail === "string" ? body.detail : `request failed (${status})`);
  }

  /** Someone else changed the record after we read it (backend error "stale_update"). */
  get isStale() {
    return this.status === 409 && this.body?.error === "stale_update";
  }
}

/** Unwraps an openapi-fetch result, throwing ApiError on failure. */
export async function unwrap<T>(
  promise: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await promise;
  if (!response.ok) {
    throw new ApiError(response.status, (error ?? {}) as ApiError["body"]);
  }
  return data as T;
}
