import { proxyMagicForge } from "@/lib/api/upstream"

export async function POST(
  request: Request,
  context: RouteContext<"/api/magicforge/governance/corpora/[id]/activate">
) {
  const { id } = await context.params
  return proxyMagicForge(request, `/corpora/${encodeURIComponent(id)}/activate`, {
    method: "POST",
    body: await request.arrayBuffer(),
  })
}
