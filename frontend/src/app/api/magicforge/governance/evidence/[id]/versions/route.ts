import { proxyMagicForge } from "@/lib/api/upstream"

export async function GET(
  request: Request,
  context: RouteContext<"/api/magicforge/governance/evidence/[id]/versions">
) {
  const { id } = await context.params
  const { search } = new URL(request.url)
  return proxyMagicForge(
    request,
    `/evidence/${encodeURIComponent(id)}/versions${search}`
  )
}
