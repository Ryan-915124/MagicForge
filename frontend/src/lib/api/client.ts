import type {
  ApiErrorCode,
  ApiErrorPayload,
  AuthenticatedActor,
  CorpusStatsResponse,
  EvidenceCard,
  FastApiErrorPayload,
  GenerationResponse,
  HealthResponse,
  KnowledgeNodeVersion,
  KnowledgeSearchFilters,
  KnowledgeSearchResponse,
  LoginResponse,
  LogoutResponse,
  ResearchConsoleResponse,
} from "@/lib/api/types"
import { readCsrfToken } from "@/lib/auth/csrf"

export const AUTH_INVALIDATED_EVENT = "magicforge:auth-invalidated"

export class MagicForgeApiError extends Error {
  constructor(
    message: string,
    public readonly code: ApiErrorCode,
    public readonly status: number,
    public readonly upstreamPath?: string
  ) {
    super(message)
    this.name = "MagicForgeApiError"
  }
}

function isApiErrorPayload(value: unknown): value is ApiErrorPayload {
  if (typeof value !== "object" || value === null || !("error" in value)) return false
  const error = value.error
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    typeof error.code === "string" &&
    "message" in error &&
    typeof error.message === "string"
  )
}

function isFastApiErrorPayload(value: unknown): value is FastApiErrorPayload {
  if (typeof value !== "object" || value === null || !("detail" in value)) return false
  const detail = value.detail
  return (
    typeof detail === "string" ||
    (typeof detail === "object" && detail !== null)
  )
}

function apiErrorFromPayload(data: unknown) {
  if (isApiErrorPayload(data)) return data.error
  if (!isFastApiErrorPayload(data)) return null
  if (typeof data.detail === "string") {
    return { code: "backend_error" as ApiErrorCode, message: data.detail }
  }
  return {
    code: (data.detail.code || "backend_error") as ApiErrorCode,
    message: data.detail.message || "MagicForge API request failed.",
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  })

  const data = (await response.json().catch(() => null)) as
    | T
    | ApiErrorPayload
    | FastApiErrorPayload
    | null
  if (!response.ok) {
    const error = apiErrorFromPayload(data)
    if (
      typeof window !== "undefined" &&
      response.status === 401 &&
      !path.startsWith("/api/magicforge/auth/")
    ) {
      window.dispatchEvent(
        new CustomEvent(AUTH_INVALIDATED_EVENT, {
          detail: { status: response.status },
        })
      )
    }
    throw new MagicForgeApiError(
      error?.message ?? "MagicForge API request failed.",
      error?.code ?? "backend_error",
      response.status,
      error?.upstream_path
    )
  }

  return data as T
}

export { request as magicForgeRequest }

function buildSearchParams(filters: KnowledgeSearchFilters) {
  const params = new URLSearchParams()
  params.set("query", filters.query)
  if (filters.limit) params.set("limit", String(filters.limit))

  const listFields = [
    "knowledge_types",
    "domains",
    "ontology_paths",
    "knowledge_origins",
    "evidence_levels",
    "entity_ids",
    "entity_types",
    "relation_types",
  ] as const

  for (const field of listFields) {
    for (const value of filters[field] ?? []) params.append(field, value)
  }
  return params
}

export const magicForgeApi = {
  login(identifier: string, password: string) {
    return request<LoginResponse>("/api/magicforge/auth/login", {
      method: "POST",
      cache: "no-store",
      body: JSON.stringify({
        identifier,
        password,
        transport: "cookie",
        device_label: "MagicForge web instrument",
      }),
    })
  },

  me() {
    return request<AuthenticatedActor>("/api/magicforge/auth/me", {
      cache: "no-store",
    })
  },

  logout() {
    const csrfToken = readCsrfToken()
    if (!csrfToken) {
      return Promise.reject(
        new MagicForgeApiError(
          "The browser session is missing its CSRF token. Sign in again.",
          "csrf_cookie_missing",
          403
        )
      )
    }
    return request<LogoutResponse>("/api/magicforge/auth/logout", {
      method: "POST",
      cache: "no-store",
      headers: { "X-CSRF-Token": csrfToken },
    })
  },

  chat(question: string) {
    return request<GenerationResponse>("/api/magicforge/chat", {
      method: "POST",
      body: JSON.stringify({ question }),
    })
  },

  health() {
    return request<HealthResponse>("/api/magicforge/health", { cache: "no-store" })
  },

  search(filters: KnowledgeSearchFilters) {
    return request<KnowledgeSearchResponse>(
      `/api/magicforge/knowledge/search?${buildSearchParams(filters)}`,
      { cache: "no-store" }
    )
  },

  knowledgeNode(id: string) {
    return request<KnowledgeNodeVersion>(`/api/magicforge/knowledge/node/${encodeURIComponent(id)}`)
  },

  evidence(id: string) {
    return request<EvidenceCard>(`/api/magicforge/evidence/${encodeURIComponent(id)}`)
  },

  stats() {
    return request<CorpusStatsResponse>("/api/magicforge/stats", { cache: "no-store" })
  },

  researchConsole() {
    return request<ResearchConsoleResponse>("/api/magicforge/research/console", {
      cache: "no-store",
    })
  },
}
