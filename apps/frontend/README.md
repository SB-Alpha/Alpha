# frontend

운영자 화면입니다. **Next.js 16 + TailwindCSS 4** (pnpm).

- `/` 이전 요청 입력 (`POST /runs`) · 최근 run 목록
- `/runs/{id}` 단계 진행 상황, 승인 ① 계획(요약 3줄 + 필수 조건), 승인 ② 전환, 수동 입력, 재시도, 중단 (2초마다 상태 조회)

## 실행

```bash
# 1) 백엔드 (apps/backend) — uvicorn worker 1개
uvicorn app.main:app --port 8000

# 2) 프론트
cd apps/frontend
pnpm install
pnpm dev            # http://localhost:3000
```

- 브라우저는 `/api/*`를 부르고 Next.js가 `WORKFLOWS_API_URL`(기본 `http://127.0.0.1:8000`)로 넘깁니다.
- 다른 주소의 백엔드를 쓰려면 `WORKFLOWS_API_URL=http://<host>:<port> pnpm dev`.

## 화면 규칙

- 모듈이 값을 주지 않으면 "미제공"으로 표시합니다. 0이나 성공으로 채우지 않습니다.
- 수동으로 넘긴 단계는 `수동` 표시가 붙습니다.
- 승인 ① 버튼: planning이 `approvable=false`를 주면 막고, 값이 없으면 경고와 함께 엽니다 (`lib/plan.ts`의 `canApprovePlan`, 팀 합의 전 기본값).

## 구조

- `lib/workflows.ts`: workflows API 클라이언트·타입 (backend `workflows/schemas.py`와 같은 모양)
- `lib/plan.ts`: planning 결과 읽기, 승인 ① 허용 규칙
- `components/`: 단계 표시, 승인 ①, 운영자 조작
