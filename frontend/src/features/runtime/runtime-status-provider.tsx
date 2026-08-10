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

import { MagicForgeApiError, magicForgeApi } from "@/lib/api/client"
import type { HealthResponse } from "@/lib/api/types"

type RuntimeStatus = "loading" | "ready" | "unavailable"

interface RuntimeStatusContextValue {
  error: MagicForgeApiError | null
  health: HealthResponse | null
  refresh: () => Promise<void>
  status: RuntimeStatus
}

const RuntimeStatusContext = createContext<RuntimeStatusContextValue | null>(null)

export function RuntimeStatusProvider({ children }: { children: ReactNode }) {
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [error, setError] = useState<MagicForgeApiError | null>(null)
  const [status, setStatus] = useState<RuntimeStatus>("loading")
  const requestInFlight = useRef<Promise<void> | null>(null)

  const refresh = useCallback(async () => {
    if (requestInFlight.current) return requestInFlight.current

    const operation = (async () => {
      setStatus("loading")
      try {
        const response = await magicForgeApi.health()
        setHealth(response)
        setError(null)
        setStatus("ready")
      } catch (cause) {
        setHealth(null)
        setError(
          cause instanceof MagicForgeApiError
            ? cause
            : new MagicForgeApiError(
                "Runtime status is unavailable.",
                "backend_error",
                500
              )
        )
        setStatus("unavailable")
      }
    })()

    requestInFlight.current = operation
    try {
      await operation
    } finally {
      if (requestInFlight.current === operation) requestInFlight.current = null
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const value = useMemo(
    () => ({ error, health, refresh, status }),
    [error, health, refresh, status]
  )

  return (
    <RuntimeStatusContext.Provider value={value}>
      {children}
    </RuntimeStatusContext.Provider>
  )
}

export function useRuntimeStatus() {
  const context = useContext(RuntimeStatusContext)
  if (!context) {
    throw new Error("useRuntimeStatus must be used within RuntimeStatusProvider")
  }
  return context
}
