import { proxyMagicForge } from "@/lib/api/upstream"

export async function POST(
  request: Request,
  context: RouteContext<"/api/magicforge/governance/storage/manifests/[id]/authorize">
) {
  const { id } = await context.params
  return proxyMagicForge(
    request,
    `/storage/manifests/${encodeURIComponent(id)}/authorize`,
    {
      method: "POST",
      body: await request.arrayBuffer(),
    }
  )
}
