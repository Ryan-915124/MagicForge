import { proxyMagicForge } from "@/lib/api/upstream"

export async function GET(
  request: Request,
  context: RouteContext<"/api/magicforge/evidence/[id]">
) {
  const { id } = await context.params
  return proxyMagicForge(request, `/evidence/${encodeURIComponent(id)}`)
}
