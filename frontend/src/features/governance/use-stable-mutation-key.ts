"use client"

import { useCallback, useEffect, useRef } from "react"

function fingerprint(payload: unknown) {
  return JSON.stringify(payload)
}

export function useStableMutationKey(scope: string) {
  const attempt = useRef<{ scope: string; fingerprint: string; key: string } | null>(null)

  useEffect(() => {
    attempt.current = null
  }, [scope])

  const keyFor = useCallback(
    (payload: unknown) => {
      const nextFingerprint = fingerprint(payload)
      if (attempt.current?.scope === scope && attempt.current.fingerprint === nextFingerprint) {
        return attempt.current.key
      }
      const key = `${scope}:${crypto.randomUUID()}`
      attempt.current = { scope, fingerprint: nextFingerprint, key }
      return key
    },
    [scope]
  )

  const clear = useCallback(() => {
    attempt.current = null
  }, [])

  return { keyFor, clear }
}
