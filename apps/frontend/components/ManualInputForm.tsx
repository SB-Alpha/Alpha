"use client";

import { useState } from "react";
import { STAGE_LABEL } from "@/lib/labels";
import { BUTTON_PRIMARY, INPUT } from "@/lib/ui";
import type { StageName } from "@/lib/workflows";

type Props = {
  stage: StageName;
  isBusy: boolean;
  onSubmit: (output: Record<string, unknown>, note: string) => void;
};

function parseObject(text: string): Record<string, unknown> | string {
  if (text.trim() === "") return "결과 JSON 을 넣어 주세요.";
  try {
    const value: unknown = JSON.parse(text);
    if (typeof value === "object" && value !== null && !Array.isArray(value)) {
      return value as Record<string, unknown>;
    }
    return "JSON 객체({ ... })를 넣어 주세요.";
  } catch {
    return "JSON 형식이 올바르지 않습니다.";
  }
}

// 부모가 key={stage} 로 그려서, 단계가 넘어가면 입력값이 비워집니다.
export function ManualInputForm({ stage, isBusy, onSubmit }: Props) {
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const [jsonError, setJsonError] = useState<string | null>(null);

  const handleSubmit = () => {
    const parsed = parseObject(text);
    if (typeof parsed === "string") {
      setJsonError(parsed);
      return;
    }
    setJsonError(null);
    onSubmit(parsed, note);
  };

  return (
    <div className="rounded-xl border border-dashed border-ink/30 bg-white p-5">
      <h3 className="font-semibold">수동 입력 — {STAGE_LABEL[stage]}</h3>
      <p className="mt-1 text-sm text-ink/60">
        모듈이 아직 연결되지 않았거나 실패했을 때, 이 단계의 결과(fixture JSON)를 넣고 다음 단계로 넘깁니다. 기록에{" "}
        <b>수동</b>으로 남습니다.
      </p>
      <label htmlFor="manual-json" className="mt-3 block text-xs font-semibold text-ink/50">
        결과 JSON
      </label>
      <textarea
        id="manual-json"
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={6}
        placeholder='{"db": {"engine": "postgres"}}'
        spellCheck={false}
        className={`${INPUT} mt-1 font-mono`}
        aria-invalid={jsonError !== null}
        aria-describedby={jsonError ? "manual-json-error" : undefined}
      />
      {jsonError && (
        <p id="manual-json-error" className="mt-1 text-sm text-rose-700">
          {jsonError}
        </p>
      )}
      <div className="mt-2 flex flex-col gap-2 sm:flex-row">
        <input
          aria-label="수동 입력 메모"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="메모 (예: plan.sample.json)"
          className={INPUT}
        />
        <button type="button" disabled={isBusy} onClick={handleSubmit} className={`${BUTTON_PRIMARY} shrink-0`}>
          결과 넣고 진행
        </button>
      </div>
    </div>
  );
}
