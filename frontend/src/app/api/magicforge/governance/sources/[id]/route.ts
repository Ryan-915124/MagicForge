import { proxyMagicForge } from "@/lib/api/upstream"

export async function GET(
  request: Request,
  context: RouteContext<"/api/magicforge/governance/sources/[id]">
) {
  const { id } = await context.params
  const { search } = new URL(request.url)
  return proxyMagicForge(
    request,
    `/sources/${encodeURIComponent(id)}${search}`
  )
}
