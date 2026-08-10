import { proxyMagicForge } from "@/lib/api/upstream"

export async function GET(request: Request) {
  const { search } = new URL(request.url)
  return proxyMagicForge(request, `/review/mappings${search}`)
}
