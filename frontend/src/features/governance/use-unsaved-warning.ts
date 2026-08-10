"use client"

import { useCallback, useEffect, useRef, useState } from "react"

import {
  GOVERNANCE_URL_CHANGE_EVENT,
  restoreGovernanceUrl,
} from "./governance-url-state"

/**
 * Protects governance drafts from refresh/tab close, in-app links, and browser
 * history navigation. Browsers intentionally render their own localized copy
 * for beforeunload; same-document navigation uses the supplied message.
 */
export function useUnsavedWarning(dirty: boolean, message: string) {
  const acceptedUrlRef = useRef("")

  useEffect(() => {
    if (!dirty) return

    acceptedUrlRef.current = `${window.location.pathname}${window.location.search}${window.location.hash}`

    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = message
      return message
    }

    const handleProgrammaticChange = () => {
      acceptedUrlRef.current = `${window.location.pathname}${window.location.search}${window.location.hash}`
    }

    const handleDocumentClick = (event: MouseEvent) => {
      if (
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      ) return

      const target = event.target
      if (!(target instanceof Element)) return
      const anchor = target.closest("a[href]")
      if (!(anchor instanceof HTMLAnchorElement) || anchor.target === "_blank" || anchor.hasAttribute("download")) return

      const destination = new URL(anchor.href, window.location.href)
      if (destination.href === window.location.href) return
      if (window.confirm(message)) return

      event.preventDefault()
      event.stopImmediatePropagation()
    }

    const handlePopState = () => {
      const nextUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`
      if (!acceptedUrlRef.current || nextUrl === acceptedUrlRef.current) return
      if (window.confirm(message)) {
        acceptedUrlRef.current = nextUrl
        return
      }
      restoreGovernanceUrl(acceptedUrlRef.current)
    }

    window.addEventListener("beforeunload", handleBeforeUnload)
    window.addEventListener("popstate", handlePopState)
    window.addEventListener(GOVERNANCE_URL_CHANGE_EVENT, handleProgrammaticChange)
    document.addEventListener("click", handleDocumentClick, true)
    return () => {
      window.removeEventListener("beforeunload", handleBeforeUnload)
      window.removeEventListener("popstate", handlePopState)
      window.removeEventListener(GOVERNANCE_URL_CHANGE_EVENT, handleProgrammaticChange)
      document.removeEventListener("click", handleDocumentClick, true)
    }
  }, [dirty, message])
}

export function useGuardedOpenChange({
  dirty,
  message,
  onOpenChange,
}: {
  dirty: boolean
  message: string
  onOpenChange: (open: boolean) => void
}) {
  const [discardPromptOpen, setDiscardPromptOpen] = useState(false)

  useUnsavedWarning(dirty, message)

  const guardedOnOpenChange = useCallback((nextOpen: boolean) => {
    if (!nextOpen && dirty) {
      setDiscardPromptOpen(true)
      return
    }
    onOpenChange(nextOpen)
  }, [dirty, onOpenChange])

  const confirmDiscard = useCallback(() => {
    setDiscardPromptOpen(false)
    onOpenChange(false)
  }, [onOpenChange])

  return {
    guardedOnOpenChange,
    discardPromptOpen,
    setDiscardPromptOpen,
    confirmDiscard,
  }
}
