import { Suspense } from "react";
import { CdcHome } from "@/components/cdc/CdcHome";

// db_migration 의 /db-migration/* 를 직접 부르는 화면입니다 (UI_API.md).
export default function DbMigrationPage() {
  return (
    <Suspense fallback={<p className="text-ink/50">불러오는 중…</p>}>
      <CdcHome />
    </Suspense>
  );
}
