const DEFAULT_CSRF_COOKIE_NAME = "magicforge_csrf"

export const csrfCookieName =
  process.env.NEXT_PUBLIC_MAGICFORGE_CSRF_COOKIE_NAME || DEFAULT_CSRF_COOKIE_NAME

export function readCsrfToken(): string | null {
  if (typeof document === "undefined") return null

  const encodedName = `${encodeURIComponent(csrfCookieName)}=`
  const match = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(encodedName))

  if (!match) return null
  const encodedValue = match.slice(encodedName.length)
  try {
    return decodeURIComponent(encodedValue)
  } catch {
    return null
  }
}
