import { proxyMagicForge } from "@/lib/api/upstream"

export async function GET(
  request: Request,
  context: RouteContext<"/api/magicforge/knowledge/node/[id]">
) {
  const { id } = await context.params
  return proxyMagicForge(request, `/knowledge/node/${encodeURIComponent(id)}`)
}
