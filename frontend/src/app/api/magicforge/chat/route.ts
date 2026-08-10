import { NextRequest, NextResponse } from "next/server"

import { proxyMagicForge } from "@/lib/api/upstream"

const CHAT_UPSTREAM_TIMEOUT_MS = 180_000

// Keep the route runtime budget slightly above the explicit upstream timeout.
export const maxDuration = 190

export async function POST(request: NextRequest) {
  const body = (await request.json().catch(() => null)) as { question?: unknown } | null
  const question = typeof body?.question === "string" ? body.question.trim() : ""

  if (!question) {
    return NextResponse.json(
      { error: { code: "invalid_request", message: "question must be a non-empty string." } },
      { status: 400 }
    )
  }

  return proxyMagicForge(
    request,
    "/assistant",
    {
      method: "POST",
      body: JSON.stringify({ question }),
    },
    CHAT_UPSTREAM_TIMEOUT_MS
  )
}
