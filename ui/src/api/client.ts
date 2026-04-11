export interface ConnectRequest {
  host: string;
  port: number;
  database: string;
  username: string;
  password: string;
}

export interface ConnectResponse {
  token: string;
  databases: string[];
}

export interface ConnectDefaults {
  host: string;
  port: number;
  database: string;
  username: string;
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
  warnings: string[];
}

export interface ExecuteResponse {
  results: unknown[];
  aql: string;
  bind_vars: Record<string, unknown>;
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
  return { Authorization: `Bearer ${token}` };
}

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const { headers: extraHeaders, ...rest } = options;
  const res = await fetch(path, {
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
