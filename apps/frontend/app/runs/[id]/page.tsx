import { Suspense } from "react";
import { RunView } from "@/components/RunView";

// run 화면은 URL 의 id 로 2초마다 상태를 불러오는 클라이언트 화면입니다.
export default function RunPage() {
  return (
    <Suspense fallback={<p className="text-ink/50">불러오는 중…</p>}>
      <RunView />
    </Suspense>
  );
}
