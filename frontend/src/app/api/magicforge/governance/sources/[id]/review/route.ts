import { proxyMagicForge } from "@/lib/api/upstream"

export async function POST(
  request: Request,
  context: RouteContext<"/api/magicforge/governance/sources/[id]/review">
) {
  const { id } = await context.params
  return proxyMagicForge(request, `/sources/${encodeURIComponent(id)}/review`, {
    method: "POST",
    body: await request.arrayBuffer(),
  })
}
