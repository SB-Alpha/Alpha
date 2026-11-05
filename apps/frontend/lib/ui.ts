// 버튼·입력 공통 스타일. 주 버튼은 잉크색, 위험한 동작은 빨강 테두리로 구분합니다.
export const BUTTON_PRIMARY =
  "inline-flex items-center justify-center rounded-md bg-ink px-4 py-2 text-sm font-medium text-paper transition-[transform,opacity] hover:-translate-y-px active:translate-y-0 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:translate-y-0";
export const BUTTON_SECONDARY =
  "inline-flex items-center justify-center rounded-md border border-ink/25 bg-white px-4 py-2 text-sm font-medium transition-colors hover:border-ink disabled:cursor-not-allowed disabled:opacity-40";
export const BUTTON_DANGER =
  "inline-flex items-center justify-center rounded-md border border-rose-300 bg-white px-4 py-2 text-sm font-medium text-rose-700 transition-colors hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-40";
export const INPUT =
  "w-full rounded-md border border-ink/20 bg-paper px-3 py-2 text-sm placeholder:text-ink/35 focus:border-ink focus:outline-none";
