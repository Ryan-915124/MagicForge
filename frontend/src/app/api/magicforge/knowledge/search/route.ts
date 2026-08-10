import { NextRequest } from "next/server"

import { proxyMagicForge } from "@/lib/api/upstream"

export async function GET(request: NextRequest) {
  return proxyMagicForge(request, `/knowledge/search${request.nextUrl.search}`)
}
