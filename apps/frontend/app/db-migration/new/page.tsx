import { Suspense } from "react";
import { NewRunForm } from "@/components/cdc/NewRunForm";

export default function NewDbMigrationPage() {
  return (
    <Suspense fallback={<p className="text-ink/50">불러오는 중…</p>}>
      <NewRunForm />
    </Suspense>
  );
}
