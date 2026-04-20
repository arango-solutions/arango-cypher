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
}

export interface ExplainResponse {
  aql: string;
  bind_vars: Record<string, unknown>;
  plan: unknown;
}

export interface ProfileResponse {
  aql: string;
  bind_vars: Record<string, unknown>;
  results: unknown[];
  statistics: Record<string, unknown>;
  profile: unknown;
}

function authHeaders(token: string): Record<string, string> {
  // Use a custom header — the ArangoDB platform proxy strips Authorization:Bearer
  // (it uses that header for its own JWT auth) before forwarding to the container.
  return { "X-Arango-Session": token };
}

// The SPA is served at …/frontend/. API endpoints live one level up at …/arango-cypher/.
// Root-relative fetch("/connect") would hit the domain root (ArangoDB itself) instead
// of the service. Strip "/frontend[/…]" from the current pathname to get the right prefix:
//   /_service/uds_db/<db>/<instance>/frontend/ → /_service/uds_db/<db>/<instance>
//   /frontend/ (localhost)                     → ""
function apiBase(): string {
  const idx = window.location.pathname.indexOf("/frontend");
  return idx >= 0 ? window.location.pathname.slice(0, idx) : "";
}

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

export async function nl2Cypher(
  question: string,
  mapping: Record<string, unknown>,
  useLlm: boolean = true,
): Promise<NL2CypherResponse> {
  return request("/nl2cypher", {
    method: "POST",
    body: JSON.stringify({ question, mapping, use_llm: useLlm }),
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
): Promise<NL2AqlResponse> {
  return request("/nl2aql", {
    method: "POST",
    body: JSON.stringify({ question, mapping }),
  });
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

export interface IntrospectResult {
  entities: IntrospectEntity[];
  relationships: IntrospectRelationship[];
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
