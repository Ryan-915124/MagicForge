const DEFAULT_SESSION_COOKIE_NAME = "magicforge_session"
const DEFAULT_CSRF_COOKIE_NAME = "magicforge_csrf"
const COOKIE_NAME_PATTERN = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/

export interface AuthCookieForwardingConfig {
  sessionName: string
  csrfName: string
  requireSecure: boolean
}

function configuredCookieName(value: string | undefined, fallback: string) {
  const candidate = value?.trim()
  return candidate && COOKIE_NAME_PATTERN.test(candidate) ? candidate : fallback
}

export function cookieForwardingConfigFromEnv(
  environment: Record<string, string | undefined> = process.env
): AuthCookieForwardingConfig {
  return {
    sessionName: configuredCookieName(
      environment.MAGICFORGE_SESSION_COOKIE_NAME,
      DEFAULT_SESSION_COOKIE_NAME
    ),
    csrfName: configuredCookieName(
      environment.MAGICFORGE_CSRF_COOKIE_NAME ||
        environment.NEXT_PUBLIC_MAGICFORGE_CSRF_COOKIE_NAME,
      DEFAULT_CSRF_COOKIE_NAME
    ),
    requireSecure:
      environment.MAGICFORGE_REQUIRE_SECURE_COOKIES === "true" ||
      (environment.NODE_ENV === "production" &&
        environment.MAGICFORGE_REQUIRE_SECURE_COOKIES !== "false"),
  }
}

export function selectMagicForgeRequestCookies(
  rawCookie: string | null,
  config: AuthCookieForwardingConfig
) {
  if (!rawCookie) return ""
  const allowed = new Set([config.sessionName, config.csrfName])
  return rawCookie
    .split(";")
    .map((part) => part.trim())
    .filter((part) => {
      const separator = part.indexOf("=")
      return separator > 0 && allowed.has(part.slice(0, separator).trim())
    })
    .join("; ")
}

export function isAllowedMagicForgeSetCookie(
  cookie: string,
  config: AuthCookieForwardingConfig
) {
  if (!cookie || /[\r\n]/.test(cookie)) return false
  const sections = cookie.split(";").map((part) => part.trim())
  const separator = sections[0]?.indexOf("=") ?? -1
  if (separator <= 0) return false
  const name = sections[0].slice(0, separator).trim()
  if (name !== config.sessionName && name !== config.csrfName) return false

  const attributes = sections.slice(1).map((attribute) => attribute.toLowerCase())
  const hasAttribute = (attribute: string) =>
    attributes.some((candidate) => candidate === attribute)
  const hasPrefix = (prefix: string) =>
    attributes.some((candidate) => candidate.startsWith(prefix))
  const maxAge = attributes.find((attribute) => attribute.startsWith("max-age="))
  const maxAgeValue = maxAge?.slice("max-age=".length)
  const validMaxAge = Boolean(maxAgeValue && /^-?\d+$/.test(maxAgeValue))
  const expires = attributes.find((attribute) => attribute.startsWith("expires="))
  const expiresAt = expires ? Date.parse(expires.slice("expires=".length)) : Number.NaN
  const deleting = Boolean(
    (validMaxAge && Number(maxAgeValue) <= 0) ||
      (!maxAge && Number.isFinite(expiresAt) && expiresAt <= Date.now())
  )
  const sameSite = attributes.find((attribute) => attribute.startsWith("samesite="))

  if (!hasAttribute("path=/") || !["samesite=lax", "samesite=strict"].includes(sameSite || "")) {
    return false
  }
  if (hasPrefix("domain=") || (config.requireSecure && !hasAttribute("secure"))) {
    return false
  }
  if (name === config.sessionName && !deleting && !hasAttribute("httponly")) {
    return false
  }
  if (name === config.csrfName && hasAttribute("httponly")) return false
  return true
}
