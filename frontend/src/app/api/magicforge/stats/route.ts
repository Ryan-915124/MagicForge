import { proxyMagicForge } from "@/lib/api/upstream"

export async function GET(request: Request) {
  return proxyMagicForge(request, "/stats")
}
