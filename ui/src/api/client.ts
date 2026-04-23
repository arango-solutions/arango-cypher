export interface ConnectRequest {
  url: string;
  database: string;
  username: string;
  password: string;
}

export interface ConnectResponse {
  token: string;
  databases: string[];
}

export interface ConnectDefaults {
  url: string;
  database: string;
  username: string;
  password?: string;
}

export interface TranslateRequest {
  cypher: string;
  mapping: Record<string, unknown>;
  params?: Record<string, unknown>;
  extensions_enabled?: boolean;
}

export interface TranslateResponse {
  aql: string;
  bind_vars: Record<string, unknown>;
  warnings: Array<{ message: string }>;
  elapsed_ms?: number;
}

export interface ExecuteResponse {
  results: unknown[];
  aql: string;
  bind_vars: Record<string, unknown>;
  warnings: Array<{ message: string }>;
  exec_ms?: number;
  translate_ms?: number;
}

export interface ExplainResponse {
  aql: string;
  bind_vars: Record<string, unknown>;
  plan: unknown;
  translate_ms?: number;
}

export interface ProfileResponse {
  aql: string;
  bind_vars: Record<string, unknown>;
  results: unknown[];
  statistics: Record<string, unknown>;
  profile: unknown;
  translate_ms?: number;
}

function authHeaders(token: string): Record<string, string> {
  // Use a custom header — the ArangoDB platform proxy strips Authorization:Bearer
  // (it uses that header for its own JWT auth) before forwarding to the container.
  return { "X-Arango-Session": token };
}

// The SPA is served at …/frontend/ (AMP) or …/ui/ (legacy / local-dev). API
// endpoints live one level up. Root-relative fetch("/connect") would hit the
// domain root (ArangoDB itself) instead of the service. Strip whichever prefix
// the SPA is currently mounted under to get the right API base:
//   /_service/uds_db/<db>/<instance>/frontend/ → /_service/uds_db/<db>/<instance>
//   /frontend/ (AMP localhost)                 → ""
//   /ui/ (legacy / local-dev)                  → ""
// We check /frontend first because AMP is the production deploy target.
function apiBase(): string {
  for (const prefix of ["/frontend", "/ui"]) {
    const idx = window.location.pathname.indexOf(prefix);
    if (idx >= 0) return window.location.pathname.slice(0, idx);
  }
  return "";
}

// Shown in the UI whenever the backend returns 401. The raw
// `{"message":"Unauthorized"}` payload from ArangoDB / the platform
// proxy is technically correct but reads as a cryptic error; the
// session has simply expired (tokens are short-lived) and the user
// needs to sign in again.
export const AUTH_EXPIRED_MESSAGE =
  "Your session has expired. Please re-authenticate to the database.";

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const { headers: extraHeaders, ...rest } = options;
  const res = await fetch(apiBase() + path, {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...(extraHeaders as Record<string, string>),
    },
  });
  if (!res.ok) {
    if (res.status === 401) {
      // Drain the body so the connection is released, but don't
      // bother surfacing its contents — the generic re-auth prompt
      // is more useful than e.g. "Unauthorized" or "token expired".
      await res.text().catch(() => "");
      throw new ApiError(401, AUTH_EXPIRED_MESSAGE);
    }
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, body.detail ?? body);
  }
  return res.json();
}

function formatDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const obj = detail as Record<string, unknown>;
    if (typeof obj.error === "string") return obj.error;
    if (typeof obj.detail === "string") return obj.detail;
    // ArangoDB / the AMP proxy returns `{"message": "..."}` on
    // auth and some other errors — treat `message` the same as
    // `detail`/`error` so it isn't rendered as raw JSON.
    if (typeof obj.message === "string") return obj.message;
  }
  return JSON.stringify(detail);
}

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    super(formatDetail(detail));
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export function isAuthError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401;
}

export async function getConnectDefaults(): Promise<ConnectDefaults> {
  return request("/connect/defaults");
}

export async function connect(req: ConnectRequest): Promise<ConnectResponse> {
  return request("/connect", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function disconnect(token: string): Promise<void> {
  await request("/disconnect", {
    method: "POST",
    headers: authHeaders(token),
  });
}

export async function translateCypher(
  req: TranslateRequest,
): Promise<TranslateResponse> {
  return request("/translate", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function executeCypher(
  req: TranslateRequest,
  token: string,
): Promise<ExecuteResponse> {
  return request("/execute", {
    method: "POST",
    body: JSON.stringify(req),
    headers: authHeaders(token),
  });
}

export async function explainCypher(
  req: TranslateRequest,
  token: string,
): Promise<ExplainResponse> {
  return request("/explain", {
    method: "POST",
    body: JSON.stringify(req),
    headers: authHeaders(token),
  });
}

export async function profileCypher(
  req: TranslateRequest,
  token: string,
): Promise<ProfileResponse> {
  return request("/aql-profile", {
    method: "POST",
    body: JSON.stringify(req),
    headers: authHeaders(token),
  });
}

export async function getCypherProfile(): Promise<Record<string, unknown>> {
  return request("/cypher-profile");
}

export interface SampleQuery {
  id: string;
  description: string;
  cypher: string;
  dataset: string;
  expected_min_count?: number;
}

export async function getSampleQueries(
  dataset?: string,
): Promise<{ queries: SampleQuery[] }> {
  const qs = dataset ? `?dataset=${encodeURIComponent(dataset)}` : "";
  return request(`/sample-queries${qs}`);
}

export interface NL2CypherResponse {
  cypher: string;
  explanation: string;
  confidence: number;
  method: string;
  elapsed_ms?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
}

export interface TenantContext {
  property: string;
  value: string;
  display?: string;
}

export interface NL2CypherOptions {
  useLlm?: boolean;
  useFewshot?: boolean;
  useEntityResolution?: boolean;
  sessionToken?: string;
  tenantContext?: TenantContext | null;
  // WP-29 Part 4 / WP-30: optional retry hint forwarded to the LLM
  // prompt builder (seeds ``retry_context`` on the first attempt).
  // WP-30 will drive this from the "Regenerate from NL with error
  // hint" action on translate failure.
  retryContext?: string;
}

export async function nl2Cypher(
  question: string,
  mapping: Record<string, unknown>,
  opts: NL2CypherOptions | boolean = {},
): Promise<NL2CypherResponse> {
  // Back-compat: older call sites pass `useLlm` as a bare boolean.
  const options: NL2CypherOptions =
    typeof opts === "boolean" ? { useLlm: opts } : opts;
  const body: Record<string, unknown> = { question, mapping };
  if (options.useLlm !== undefined) body.use_llm = options.useLlm;
  if (options.useFewshot !== undefined) body.use_fewshot = options.useFewshot;
  if (options.useEntityResolution !== undefined) {
    body.use_entity_resolution = options.useEntityResolution;
  }
  if (options.sessionToken) body.session_token = options.sessionToken;
  if (options.tenantContext) body.tenant_context = options.tenantContext;
  if (options.retryContext) body.retry_context = options.retryContext;
  return request("/nl2cypher", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export interface NL2AqlResponse {
  aql: string;
  bind_vars: Record<string, unknown>;
  explanation: string;
  confidence: number;
  method: string;
  elapsed_ms?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
}

export async function executeAql(
  aql: string,
  bindVars: Record<string, unknown>,
  token: string,
): Promise<ExecuteResponse> {
  return request("/execute-aql", {
    method: "POST",
    body: JSON.stringify({ aql, bind_vars: bindVars }),
    headers: authHeaders(token),
  });
}

export async function nl2Aql(
  question: string,
  mapping: Record<string, unknown>,
  tenantContext?: TenantContext | null,
): Promise<NL2AqlResponse> {
  const body: Record<string, unknown> = { question, mapping };
  if (tenantContext) body.tenant_context = tenantContext;
  return request("/nl2aql", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export interface TenantRecord {
  // Full ArangoDB document id, e.g. "Tenant/<uuid>". This is the
  // canonical tenant identifier — universal, indexed, and not
  // dependent on a schema-specific field like TENANT_HEX_ID.
  id: string;
  // Bare _key portion of `id` (the part after the slash). Used for
  // the Cypher `{_key: '...'}` shorthand in generated queries.
  key: string;
  name: string | null;
  subdomain: string | null;
  hex_id: string | null;
}

export interface TenantsResponse {
  detected: boolean;
  tenants: TenantRecord[];
  // Resolved ArangoDB collection name the catalog query was run
  // against. Surfaced so the UI can explain *why* detection
  // succeeded or failed (e.g. "looked for collection `Tenants`,
  // not found") instead of silently hiding the selector.
  collection?: string | null;
  // "client" when the UI passed an explicit collection query
  // param, "heuristic" when we fell back to the literal "Tenant"
  // name. Reported back so empty results are explainable.
  source?: "client" | "heuristic";
}

// Pluck the physical collection name backing the conceptual
// `Tenant` entity from the introspected mapping. Returns null when
// no mapping is present yet or no Tenant entity exists. Mirrors the
// transpiler's lookup (physical_mapping.entities.<Label>.collectionName)
// — we resolve client-side to keep the API a pure GET and avoid
// shipping the entire mapping back over the wire.
export function resolveTenantCollectionName(
  mapping: Record<string, unknown> | null | undefined,
): string | null {
  if (!mapping) return null;
  const pm =
    (mapping.physical_mapping as Record<string, unknown> | undefined) ??
    (mapping.physicalMapping as Record<string, unknown> | undefined);
  const ents = pm?.entities as Record<string, unknown> | undefined;
  const tenant = ents?.["Tenant"] as Record<string, unknown> | undefined;
  if (!tenant) return null;
  const coll = (tenant.collectionName ?? tenant.collection) as unknown;
  return typeof coll === "string" && coll.length > 0 ? coll : null;
}

export async function listTenants(
  token: string,
  mapping?: Record<string, unknown> | null,
): Promise<TenantsResponse> {
  // GET-only — older deployed services still understand the bare
  // `/tenants` request, so a freshly built UI talking to a
  // not-yet-restarted backend degrades to the heuristic path
  // instead of failing with 405. When we know the real collection
  // name (from the introspected mapping) we send it as a query
  // parameter so the server queries the right collection without
  // needing to receive the full mapping in the body.
  const collection = resolveTenantCollectionName(mapping);
  const path = collection
    ? `/tenants?collection=${encodeURIComponent(collection)}`
    : "/tenants";
  return request(path, { headers: authHeaders(token) });
}

export interface NlSamplesResponse {
  queries: string[];
  elapsed_ms?: number;
}

export async function suggestNlQueries(
  mapping: Record<string, unknown>,
  count: number = 8,
  useLlm: boolean = true,
): Promise<NlSamplesResponse> {
  return request("/nl-samples", {
    method: "POST",
    body: JSON.stringify({ mapping, count, use_llm: useLlm }),
  });
}

export interface IntrospectPropertyInfo {
  field: string;
  type: string;
  required?: boolean;
  sentinelValues?: string[];
  numericLike?: boolean;
  sampleValues?: string[];
}

export interface IntrospectEntity {
  label: string;
  collection: string;
  style: string;
  properties: Record<string, IntrospectPropertyInfo>;
  typeField?: string;
  typeValue?: string;
  estimatedCount?: number;
}

export interface RelationshipStatistics {
  edgeCount: number;
  avgOutDegree: number;
  avgInDegree: number;
  cardinalityPattern: string;
  selectivity: number;
}

export interface IntrospectRelationship {
  type: string;
  edgeCollection: string;
  style: string;
  domain?: string | null;
  range?: string | null;
  properties: Record<string, IntrospectPropertyInfo>;
  typeField?: string;
  typeValue?: string;
  statistics?: RelationshipStatistics;
}

export interface SchemaWarning {
  code: string;
  message: string;
  install_hint?: string;
}

export interface IntrospectResult {
  entities: IntrospectEntity[];
  relationships: IntrospectRelationship[];
  warnings?: SchemaWarning[];
}

export async function introspectSchema(
  token: string,
  sample = 50,
  force = false,
): Promise<IntrospectResult> {
  const params = new URLSearchParams({ sample: String(sample) });
  if (force) params.set("force", "true");
  return request(`/schema/introspect?${params}`, {
    headers: authHeaders(token),
  });
}

export interface ForceReacquireResult {
  source: { kind: string | null; notes: string | null };
  warnings: SchemaWarning[];
  entity_count: number;
  relationship_count: number;
}

// Hard reacquire path. Calls get_mapping(strategy="analyzer", force_refresh=True)
// on the backend, which raises ImportError (HTTP 503) when the analyzer is
// missing instead of silently returning a heuristic-built bundle. Use this
// when /schema/invalidate-cache + /schema/introspect would just re-serve a
// poisoned heuristic mapping (e.g. analyzer was installed after the cache
// was first populated).
export async function forceReacquireSchema(
  token: string,
): Promise<ForceReacquireResult> {
  return request(`/schema/force-reacquire`, {
    method: "POST",
    headers: authHeaders(token),
  });
}

export function introspectToMapping(
  result: IntrospectResult,
): Record<string, unknown> {
  const entities: Record<string, unknown>[] = [];
  const physEntities: Record<string, unknown> = {};
  const entityStats: Record<string, Record<string, unknown>> = {};
  const relStats: Record<string, Record<string, unknown>> = {};

  for (const e of result.entities) {
    const propNames = Object.keys(e.properties);
    entities.push({
      name: e.label,
      labels: [e.label],
      properties: propNames.map((p) => ({ name: p })),
    });
    const physEnt: Record<string, unknown> = {
      style: e.style || "COLLECTION",
      collectionName: e.collection,
      properties: e.properties,
    };
    if (e.typeField) {
      physEnt.typeField = e.typeField;
      physEnt.typeValue = e.typeValue;
    }
    if (e.estimatedCount != null) {
      physEnt.estimatedCount = e.estimatedCount;
      entityStats[e.label] = { estimated_count: e.estimatedCount };
    }
    physEntities[e.label] = physEnt;
  }

  const rels: Record<string, unknown>[] = [];
  const physRels: Record<string, unknown> = {};

  for (const r of result.relationships) {
    const propNames = Object.keys(r.properties);
    rels.push({
      type: r.type,
      fromEntity: r.domain || "Any",
      toEntity: r.range || "Any",
      properties: propNames.map((p) => ({ name: p })),
    });
    const physRel: Record<string, unknown> = {
      style: r.style || "DEDICATED_COLLECTION",
      edgeCollectionName: r.edgeCollection,
      domain: r.domain || undefined,
      range: r.range || undefined,
      properties: r.properties,
    };
    if (r.typeField) {
      physRel.typeField = r.typeField;
      physRel.typeValue = r.typeValue;
    }
    if (r.statistics) {
      physRel.statistics = r.statistics;
      relStats[r.type] = {
        edge_count: r.statistics.edgeCount,
        avg_out_degree: r.statistics.avgOutDegree,
        avg_in_degree: r.statistics.avgInDegree,
        cardinality_pattern: r.statistics.cardinalityPattern,
        selectivity: r.statistics.selectivity,
      };
    }
    physRels[r.type] = physRel;
  }

  const metadata: Record<string, unknown> = {};
  if (Object.keys(entityStats).length || Object.keys(relStats).length) {
    metadata.statistics = {
      entities: entityStats,
      relationships: relStats,
    };
  }

  return {
    conceptual_schema: { entities, relationships: rels },
    physical_mapping: { entities: physEntities, relationships: physRels },
    metadata,
  };
}

// ---------------------------------------------------------------------------
// Corrections (local learning)
// ---------------------------------------------------------------------------

export interface CorrectionRecord {
  id: number;
  cypher: string;
  mapping_hash: string;
  database: string;
  original_aql: string;
  corrected_aql: string;
  bind_vars: Record<string, unknown>;
  created_at: string;
  note: string;
}

export async function saveCorrection(body: {
  cypher: string;
  mapping: Record<string, unknown>;
  database?: string;
  original_aql: string;
  corrected_aql: string;
  bind_vars?: Record<string, unknown>;
  note?: string;
}): Promise<{ id: number; status: string }> {
  return request("/corrections", { method: "POST", body: JSON.stringify(body) });
}

export async function listCorrections(
  limit = 100,
): Promise<{ corrections: CorrectionRecord[] }> {
  return request(`/corrections?limit=${limit}`);
}

export async function deleteCorrection(id: number): Promise<{ status: string }> {
  return request(`/corrections/${id}`, { method: "DELETE" });
}

export async function deleteAllCorrections(): Promise<{ status: string; count: number }> {
  return request("/corrections", { method: "DELETE" });
}
