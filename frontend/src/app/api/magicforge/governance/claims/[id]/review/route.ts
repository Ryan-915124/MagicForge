import { proxyMagicForge } from "@/lib/api/upstream"

export async function POST(
  request: Request,
  context: RouteContext<"/api/magicforge/governance/claims/[id]/review">
) {
  const { id } = await context.params
  return proxyMagicForge(request, `/claims/${encodeURIComponent(id)}/review`, {
    method: "POST",
    body: await request.arrayBuffer(),
  })
}
