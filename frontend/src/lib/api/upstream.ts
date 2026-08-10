import { NextResponse } from "next/server"

import {
  cookieForwardingConfigFromEnv,
  isAllowedMagicForgeSetCookie,
} from "@/lib/api/cookie-forwarding"
import { buildMagicForgeRequestHeaders } from "@/lib/api/proxy-policy"
import type { ApiErrorCode, ApiErrorPayload } from "@/lib/api/types"

const DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"
const FORWARDED_RESPONSE_HEADERS = [
  "cache-control",
  "content-security-policy",
  "content-type",
  "cross-origin-opener-policy",
  "cross-origin-resource-policy",
  "etag",
  "location",
  "permissions-policy",
  "referrer-policy",
  "retry-after",
  "strict-transport-security",
  "vary",
  "www-authenticate",
  "x-content-type-options",
] as const

function backendBaseUrl() {
  return (process.env.MAGICFORGE_API_URL || DEFAULT_BACKEND_URL).replace(/\/$/, "")
}

function errorResponse(
  code: ApiErrorCode,
  message: string,
  status: number,
  upstreamPath: string
) {
  const payload: ApiErrorPayload = {
    error: { code, message, upstream_path: upstreamPath },
  }
  return NextResponse.json(payload, { status })
}

function setCookieValues(headers: Headers) {
  const getSetCookie = (headers as Headers & {
    getSetCookie?: () => string[]
  }).getSetCookie
  if (getSetCookie) return getSetCookie.call(headers)

  const combined = headers.get("set-cookie")
  if (!combined) return []
  return combined
    .split(/,(?=\s*[^;,\s]+=)/g)
    .map((cookie) => cookie.trim())
    .filter(Boolean)
}

export async function proxyMagicForge(
  request: Request,
  upstreamPath: string,
  init?: RequestInit,
  timeoutMs = 12_000
): Promise<NextResponse> {
  try {
    const cookieConfig = cookieForwardingConfigFromEnv()
    const headers = buildMagicForgeRequestHeaders(
      request.headers,
      cookieConfig,
      Boolean(init?.body)
    )
    new Headers(init?.headers).forEach((value, name) => headers.set(name, value))

    const response = await fetch(`${backendBaseUrl()}${upstreamPath}`, {
      ...init,
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(timeoutMs),
      headers,
    })

    const responseHeaders = new Headers()
    for (const name of FORWARDED_RESPONSE_HEADERS) {
      const value = response.headers.get(name)
      if (value) responseHeaders.set(name, value)
    }
    if (request.headers.has("cookie") || request.headers.has("authorization")) {
      responseHeaders.set("Cache-Control", "no-store")
    }
    for (const cookie of setCookieValues(response.headers).filter((value) =>
      isAllowedMagicForgeSetCookie(value, cookieConfig)
    )) {
      responseHeaders.append("Set-Cookie", cookie)
    }

    const body = await response.arrayBuffer()

    return new NextResponse(body.byteLength ? body : null, {
      status: response.status,
      headers: responseHeaders,
    })
  } catch (cause) {
    if (cause instanceof Error && cause.name === "TimeoutError") {
      const timeoutSeconds = Math.round(timeoutMs / 1_000)
      return errorResponse(
        "upstream_timeout",
        `等待 MagicForge 后端响应超过 ${timeoutSeconds} 秒，代理请求已超时；这不表示 FastAPI 无法连接。请重试。 / The MagicForge upstream response exceeded the ${timeoutSeconds}-second proxy wait limit. This request timed out; FastAPI is not being reported as unreachable. Please retry.`,
        504,
        upstreamPath
      )
    }

    return errorResponse(
      "backend_unreachable",
      "The configured FastAPI backend is unreachable. Start the backend and retry.",
      503,
      upstreamPath
    )
  }
}
