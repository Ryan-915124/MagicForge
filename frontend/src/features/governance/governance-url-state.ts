"use client"

import { useMemo, useSyncExternalStore } from "react"

type UrlValue = string | number | null | undefined
type HistoryMode = "push" | "replace"

const subscribers = new Set<() => void>()
let listening = false

export const GOVERNANCE_URL_CHANGE_EVENT = "magicforge:governance-url-change"

function emitChange() {
  for (const subscriber of subscribers) subscriber()
}

function emitProgrammaticChange() {
  emitChange()
  window.dispatchEvent(new Event(GOVERNANCE_URL_CHANGE_EVENT))
}

function handlePopState() {
  // Let dirty-draft guards observe/cancel the history transition before React
  // consumes the new URL and unmounts the active review sheet.
  queueMicrotask(emitChange)
}

function subscribe(subscriber: () => void) {
  subscribers.add(subscriber)
  if (!listening && typeof window !== "undefined") {
    window.addEventListener("popstate", handlePopState)
    listening = true
  }
  return () => {
    subscribers.delete(subscriber)
    if (listening && subscribers.size === 0 && typeof window !== "undefined") {
      window.removeEventListener("popstate", handlePopState)
      listening = false
    }
  }
}

function browserSnapshot() {
  return typeof window === "undefined" ? "" : window.location.search
}

function serverSnapshot() {
  return ""
}

export function useGovernanceSearchParams() {
  const search = useSyncExternalStore(subscribe, browserSnapshot, serverSnapshot)
  return useMemo(() => new URLSearchParams(search), [search])
}

export function updateGovernanceUrl(
  patch: Record<string, UrlValue>,
  mode: HistoryMode = "push"
) {
  if (typeof window === "undefined") return
  const params = new URLSearchParams(window.location.search)
  for (const [key, value] of Object.entries(patch)) {
    if (value === null || value === undefined || value === "") params.delete(key)
    else params.set(key, String(value))
  }
  const nextSearch = params.toString()
  const nextUrl = `${window.location.pathname}${nextSearch ? `?${nextSearch}` : ""}${window.location.hash}`
  const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`
  if (nextUrl === currentUrl) return
  window.history[mode === "push" ? "pushState" : "replaceState"](null, "", nextUrl)
  emitProgrammaticChange()
}

/** Restore an already validated same-origin location after a cancelled popstate. */
export function restoreGovernanceUrl(relativeUrl: string) {
  if (typeof window === "undefined") return
  const target = new URL(relativeUrl, window.location.origin)
  if (target.origin !== window.location.origin) return
  window.history.pushState(null, "", `${target.pathname}${target.search}${target.hash}`)
  emitProgrammaticChange()
}

export function boundedPageOffset(
  value: string | null,
  pageSize: number,
  maximum = Number.MAX_SAFE_INTEGER
) {
  if (!value) return 0
  const offset = Number(value)
  if (!Number.isSafeInteger(offset) || offset < 0 || offset > maximum || offset % pageSize !== 0) {
    return 0
  }
  return offset
}
