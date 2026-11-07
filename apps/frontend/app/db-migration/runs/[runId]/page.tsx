import { Suspense } from "react";
import { CdcRunDetail } from "@/components/cdc/CdcRunDetail";

// URL 의 runId 로 GET /db-migration/runs/{runId} 를 4초마다 읽는 클라이언트 화면입니다.
export default function CdcRunPage() {
  return (
    <Suspense fallback={<p className="text-ink/50">불러오는 중…</p>}>
      <CdcRunDetail />
    </Suspense>
  );
}
