import {
  selectMagicForgeRequestCookies,
  type AuthCookieForwardingConfig,
} from "./cookie-forwarding.ts"

const FORWARDED_REQUEST_HEADERS = [
  "authorization",
  "idempotency-key",
  "origin",
  "user-agent",
  "x-correlation-id",
  "x-csrf-token",
  "x-request-id",
] as const

export function buildMagicForgeRequestHeaders(
  incoming: Headers,
  cookieConfig: AuthCookieForwardingConfig,
  hasBody: boolean
) {
  const headers = new Headers({ Accept: "application/json" })
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = incoming.get(name)
    if (value) headers.set(name, value)
  }
  const cookie = selectMagicForgeRequestCookies(incoming.get("cookie"), cookieConfig)
  if (cookie) headers.set("Cookie", cookie)
  if (hasBody) headers.set("Content-Type", "application/json")
  return headers
}
