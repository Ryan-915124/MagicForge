import { proxyMagicForge } from "@/lib/api/upstream"

export async function GET(
  request: Request,
  context: RouteContext<"/api/magicforge/governance/storage/manifests/[id]">
) {
  const { id } = await context.params
  const { search } = new URL(request.url)
  return proxyMagicForge(
    request,
    `/storage/manifests/${encodeURIComponent(id)}${search}`
  )
}
