import { proxyMagicForge } from "@/lib/api/upstream"

export async function GET(request: Request) {
  const { search } = new URL(request.url)
  return proxyMagicForge(request, `/storage/manifests${search}`)
}

export async function POST(request: Request) {
  return proxyMagicForge(request, "/storage/manifests", {
    method: "POST",
    body: await request.arrayBuffer(),
  })
}
