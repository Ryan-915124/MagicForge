import { proxyMagicForge } from "@/lib/api/upstream"

export async function POST(request: Request) {
  return proxyMagicForge(request, "/auth/logout", { method: "POST" })
}
