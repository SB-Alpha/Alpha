import { Suspense } from "react";
import { CloudKeysPage } from "@/components/cdc/CdcHome";

export default function CloudKeysRoute() {
  return (
    <Suspense fallback={<p className="text-ink/50">불러오는 중…</p>}>
      <CloudKeysPage />
    </Suspense>
  );
}
