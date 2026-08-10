import { createHash } from "node:crypto"

import { NextRequest, NextResponse } from "next/server"

import { proxyMagicForge } from "@/lib/api/upstream"

interface LoginPayload {
  identifier?: unknown
  password?: unknown
  transport?: unknown
  device_label?: unknown
}

export const runtime = "nodejs"

const MAX_LOGIN_BODY_BYTES = 16 * 1024
const LOGIN_WINDOW_MS = 60_000
const MAX_LOGIN_ATTEMPTS = 8
const MAX_RATE_LIMIT_KEYS = 5_000
const loginAttempts = new Map<string, { count: number; resetAt: number }>()

class LoginBodyTooLargeError extends Error {}

async function readLoginPayload(request: NextRequest) {
  const reader = request.body?.getReader()
  if (!reader) return null
  const chunks: Uint8Array[] = []
  let size = 0
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    size += value.byteLength
    if (size > MAX_LOGIN_BODY_BYTES) {
      await reader.cancel()
      throw new LoginBodyTooLargeError()
    }
    chunks.push(value)
  }
  const body = new Uint8Array(size)
  let offset = 0
  for (const chunk of chunks) {
    body.set(chunk, offset)
    offset += chunk.byteLength
  }
  try {
    return JSON.parse(new TextDecoder().decode(body)) as LoginPayload
  } catch {
    return null
  }
}

function attemptKey(identifier: string) {
  return createHash("sha256").update(identifier.trim().toLocaleLowerCase("en-US")).digest("hex")
}

function reserveLoginAttempt(identifier: string) {
  const now = Date.now()
  if (loginAttempts.size >= MAX_RATE_LIMIT_KEYS) {
    for (const [key, value] of loginAttempts) {
      if (value.resetAt <= now) loginAttempts.delete(key)
    }
    if (loginAttempts.size >= MAX_RATE_LIMIT_KEYS) {
      loginAttempts.delete(loginAttempts.keys().next().value as string)
    }
  }
  const key = attemptKey(identifier)
  const current = loginAttempts.get(key)
  const attempt = !current || current.resetAt <= now
    ? { count: 1, resetAt: now + LOGIN_WINDOW_MS }
    : { count: current.count + 1, resetAt: current.resetAt }
  loginAttempts.set(key, attempt)
  return { key, ...attempt }
}

export async function POST(request: NextRequest) {
  let payload: LoginPayload | null
  try {
    payload = await readLoginPayload(request)
  } catch (cause) {
    if (!(cause instanceof LoginBodyTooLargeError)) throw cause
    return NextResponse.json(
      { error: { code: "request_too_large", message: "Login request is too large." } },
      { status: 413 }
    )
  }
  const identifier =
    typeof payload?.identifier === "string" ? payload.identifier.trim() : ""
  const password = typeof payload?.password === "string" ? payload.password : ""
  const transport = payload?.transport === "bearer" ? "bearer" : "cookie"
  const deviceLabel =
    typeof payload?.device_label === "string" && payload.device_label.trim()
      ? payload.device_label.trim()
      : undefined

  if (
    !identifier ||
    identifier.length > 320 ||
    !password ||
    password.length > 1_024 ||
    (deviceLabel?.length ?? 0) > 255
  ) {
    return NextResponse.json(
      {
        error: {
          code: "invalid_request",
          message: "identifier and password are required.",
        },
      },
      { status: 400 }
    )
  }

  const attempt = reserveLoginAttempt(identifier)
  if (attempt.count > MAX_LOGIN_ATTEMPTS) {
    const retryAfter = Math.max(1, Math.ceil((attempt.resetAt - Date.now()) / 1_000))
    return NextResponse.json(
      {
        error: {
          code: "login_rate_limited",
          message: "Too many login attempts. Wait before trying again.",
        },
      },
      { status: 429, headers: { "Retry-After": String(retryAfter) } }
    )
  }

  const response = await proxyMagicForge(request, "/auth/login", {
    method: "POST",
    body: JSON.stringify({
      identifier,
      password,
      transport,
      ...(deviceLabel ? { device_label: deviceLabel } : {}),
    }),
  })
  if (response.ok) {
    loginAttempts.delete(attempt.key)
  } else if (response.status !== 401 && response.status !== 429) {
    const current = loginAttempts.get(attempt.key)
    if (current && current.count === attempt.count) {
      current.count -= 1
      if (current.count <= 0) loginAttempts.delete(attempt.key)
    }
  }
  return response
}
