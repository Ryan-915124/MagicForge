import type { NextConfig } from "next"

const isDevelopment = process.env.NODE_ENV !== "production"
const contentSecurityPolicy = [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  "frame-ancestors 'none'",
  "frame-src 'none'",
  "form-action 'self'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  "style-src 'self' 'unsafe-inline'",
  `script-src 'self' 'unsafe-inline'${isDevelopment ? " 'unsafe-eval'" : ""}`,
  `connect-src 'self'${isDevelopment ? " ws: http://127.0.0.1:* http://localhost:*" : ""}`,
  "worker-src 'self' blob:",
].join("; ")

const nextConfig: NextConfig = {
  reactStrictMode: true,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "Content-Security-Policy", value: contentSecurityPolicy },
          { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
          { key: "Cross-Origin-Resource-Policy", value: "same-origin" },
          {
            key: "Permissions-Policy",
            value: "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
          },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
        ],
      },
    ]
  },
  // Next.js 16 blocks dev assets when the browser uses the loopback IP while
  // the dev server identifies itself as localhost. Allow the explicit local
  // addresses used by Windows/WSL without opening the dev server to all origins.
  allowedDevOrigins: ["127.0.0.1"],
  experimental: {
    // The CLI checker launches a detached child process; this workspace suppresses
    // detached stdout. The TypeScript compiler API performs the same build check.
    useTypeScriptCli: false,
  },
}

export default nextConfig
