"use client"

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react"

import {
  AUTH_INVALIDATED_EVENT,
  MagicForgeApiError,
  magicForgeApi,
} from "@/lib/api/client"
import type { ApiErrorCode, AuthenticatedActor } from "@/lib/api/types"
import { readCsrfToken } from "@/lib/auth/csrf"

type AuthStatus = "loading" | "authenticated" | "anonymous" | "unavailable"

interface AuthContextValue {
  actor: AuthenticatedActor | null
  error: string | null
  errorCode: ApiErrorCode | null
  isRefreshing: boolean
  lastCheckedAt: number | null
  verificationCount: number
  status: AuthStatus
  login: (identifier: string, password: string) => Promise<AuthenticatedActor>
  logout: () => Promise<void>
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

function isAnonymousResponse(error: unknown) {
  return error instanceof MagicForgeApiError && error.status === 401
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "MagicForge authentication is unavailable."
}

function errorCode(error: unknown): ApiErrorCode {
  return error instanceof MagicForgeApiError ? error.code : "backend_error"
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [actor, setActor] = useState<AuthenticatedActor | null>(null)
  const [status, setStatus] = useState<AuthStatus>("loading")
  const [error, setError] = useState<string | null>(null)
  const [authErrorCode, setAuthErrorCode] = useState<ApiErrorCode | null>(null)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [lastCheckedAt, setLastCheckedAt] = useState<number | null>(null)
  const [verificationCount, setVerificationCount] = useState(0)
  const refreshInFlight = useRef<Promise<void> | null>(null)

  const refresh = useCallback(async () => {
    if (refreshInFlight.current) return refreshInFlight.current

    const operation = (async () => {
      setIsRefreshing(true)
      try {
        const currentActor = await magicForgeApi.me()
        setActor(currentActor)
        setError(null)
        setAuthErrorCode(null)
        setStatus("authenticated")
      } catch (cause) {
        setActor(null)
        if (isAnonymousResponse(cause)) {
          setError(null)
          setAuthErrorCode(null)
          setStatus("anonymous")
          return
        }
        setError(errorMessage(cause))
        setAuthErrorCode(errorCode(cause))
        setStatus("unavailable")
      } finally {
        setLastCheckedAt(Date.now())
        setVerificationCount((count) => count + 1)
        setIsRefreshing(false)
      }
    })()

    refreshInFlight.current = operation
    try {
      await operation
    } finally {
      if (refreshInFlight.current === operation) refreshInFlight.current = null
    }
  }, [])

  useEffect(() => {
    let active = true
    magicForgeApi.me().then(
      (currentActor) => {
        if (!active) return
        setActor(currentActor)
        setError(null)
        setAuthErrorCode(null)
        setLastCheckedAt(Date.now())
        setVerificationCount((count) => count + 1)
        setStatus("authenticated")
      },
      (cause: unknown) => {
        if (!active) return
        setActor(null)
        if (isAnonymousResponse(cause)) {
          setError(null)
          setAuthErrorCode(null)
          setLastCheckedAt(Date.now())
          setVerificationCount((count) => count + 1)
          setStatus("anonymous")
          return
        }
        setError(errorMessage(cause))
        setAuthErrorCode(errorCode(cause))
        setLastCheckedAt(Date.now())
        setVerificationCount((count) => count + 1)
        setStatus("unavailable")
      }
    )
    return () => {
      active = false
    }
  }, [])

  useEffect(() => {
    const revalidate = () => {
      void refresh()
    }
    window.addEventListener(AUTH_INVALIDATED_EVENT, revalidate)
    return () => window.removeEventListener(AUTH_INVALIDATED_EVENT, revalidate)
  }, [refresh])

  const login = useCallback(async (identifier: string, password: string) => {
    await magicForgeApi.login(identifier, password)
    if (!readCsrfToken()) {
      throw new MagicForgeApiError(
        "The authenticated session did not provide a readable CSRF token.",
        "csrf_cookie_missing",
        403
      )
    }
    const currentActor = await magicForgeApi.me()
    setActor(currentActor)
    setError(null)
    setAuthErrorCode(null)
    setLastCheckedAt(Date.now())
    setStatus("authenticated")
    return currentActor
  }, [])

  const logout = useCallback(async () => {
    try {
      await magicForgeApi.logout()
      setActor(null)
      setError(null)
      setAuthErrorCode(null)
      setLastCheckedAt(Date.now())
      setStatus("anonymous")
    } catch (cause) {
      if (isAnonymousResponse(cause)) {
        setActor(null)
        setError(null)
        setAuthErrorCode(null)
        setLastCheckedAt(Date.now())
        setStatus("anonymous")
        return
      }
      setError(errorMessage(cause))
      throw cause
    }
  }, [])

  const value = useMemo(
    () => ({
      actor,
      error,
      errorCode: authErrorCode,
      isRefreshing,
      lastCheckedAt,
      verificationCount,
      status,
      login,
      logout,
      refresh,
    }),
    [
      actor,
      authErrorCode,
      error,
      isRefreshing,
      lastCheckedAt,
      login,
      logout,
      refresh,
      status,
      verificationCount,
    ]
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) throw new Error("useAuth must be used within AuthProvider")
  return context
}
