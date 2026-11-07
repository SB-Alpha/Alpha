import type { NextConfig } from "next";

// 브라우저는 같은 출처의 /api/* 로 부르고, Next.js 가 workflows 백엔드로 넘깁니다 (CORS 설정 불필요).
const WORKFLOWS_API_URL = process.env.WORKFLOWS_API_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  // /api 프록시 기본 제한은 30초입니다. DB 전환 승인(POST /db-migration/runs/{id}/cutover)은 최대 60초 걸립니다.
  // 수십 분 걸리는 finish/stop 은 화면이 시간 초과를 "정리 중" 으로 보고 상태를 계속 읽습니다.
  experimental: { proxyTimeout: 120_000 },
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
