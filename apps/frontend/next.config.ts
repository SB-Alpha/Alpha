import type { NextConfig } from "next";

// 브라우저는 같은 출처의 /api/* 로 부르고, Next.js 가 workflows 백엔드로 넘깁니다 (CORS 설정 불필요).
const WORKFLOWS_API_URL = process.env.WORKFLOWS_API_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${WORKFLOWS_API_URL}/:path*` }];
  },
};

export default nextConfig;
