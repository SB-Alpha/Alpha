# frontend

운영자 화면입니다. **Next.js 16 + TailwindCSS 4** (pnpm).

- `/` 이전 요청 입력 (`POST /runs`) · 최근 run 목록. 필수 입력은 서비스 이름, 자격증명 참조 2개, 목적지이고 나머지는 "고급 클라우드 설정" 에 있습니다
- `/runs/{id}` 단계 진행 상황, 승인 ① 계획(요약 3줄 + 필수 조건), 승인 ② 전환, 수동 입력, 재시도, 중단, AI 재진단, CDC 작업 긴급 조작 (2초마다 상태 조회)

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
- 입력 폼: ECS 서비스는 서비스 이름을 씁니다. RDS 는 비우면 assessment 가 계정의 인스턴스가 하나뿐일 때 고르고, 여러 개면 지정하라고 안내합니다.
- 목적지: 백엔드에 어댑터가 등록된 클라우드만 고를 수 있습니다 (`docs/TARGET_ADAPTER_SPEC.md`). 나머지는 "어댑터 준비 중" 으로 비활성화합니다.
- CDC 긴급 조작: run 이 멈췄을 때 쓰는 직접 제어라 접어 둡니다. 에이전트 규칙에 맞는 버튼만 열고 확인 창을 거칩니다. 작업 메시지에는 접속 정보가 들어갈 수 있어 상태와 단계만 표시합니다.
- 승인 ① 버튼: planning이 `approvable=false`를 줘도 막지 않고 경고와 함께 엽니다. 값이 없을 때도 경고와 함께 엽니다 (`lib/plan.ts`의 `canApprovePlan`, PR #45 팀 합의).

## 구조

- `lib/workflows.ts`: workflows API 클라이언트·타입 (backend `workflows/schemas.py`와 같은 모양)
- `lib/adapters/types.ts`: 목적지 클라우드 어댑터 (표시 이름, 기본 리전, 사용 가능 여부)
- `lib/dbMigration.ts`: CDC 작업 직접 제어 클라이언트
- `lib/plan.ts`: planning 결과 읽기, 승인 ① 허용 규칙
- `components/`: 단계 표시, 승인 ①, 운영자 조작
