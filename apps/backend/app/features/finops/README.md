# finops (초안)

이전 후보별 월 비용을 비교해서 절감액을 계산하고, 승인자가 읽을 제안 문장을 붙이는 모듈입니다.

- 모듈 담당: 박재현 (11/7 변경). 비용 산출(`estimate.py`)·절감 설명 첫 구현은 이주호

## 원칙

- **숫자와 판단은 코드가 냅니다.** `savings`·`best`, 그리고 금액·증감·추천이 들어가는 `summary` 문장은 항상 `service.py` 가 씁니다.
- **AI 는 숫자 없는 설명만 덧붙입니다.** `ALPHA_AI_ENABLED=1` 일 때만 켜지고, 규칙 주의점 뒤에 설명을 붙입니다.
  숫자가 있거나 싸다·아낀다·추천한다 같은 비용 판단이 있는 설명은 통째로 버립니다 (`ai.check_notes`).
  입력에 있는 숫자인지 대조하는 방식은 "$15(25%) 를 $60(60%) 로" 같은 틀린 조합을 못 막아서 숫자 자체를 막습니다.
  호출이 실패하거나 설명이 비어도 규칙 결과 그대로입니다. 규칙 주의점은 AI 가 지울 수 없습니다.
- **모르는 값은 0 이 아니라 None("미제공").** 다음 경우는 절감액을 내지 않고 추천 후보에서도 뺍니다.
  일부만 더한 합계로 비교하면 실제보다 싸 보이기 때문입니다.
  - 원본이나 후보에 금액이 빠진 항목이 있을 때. 원본 합계도 "현재 월 비용" 대신 "미제공 (아는 항목 합계 …)" 로 씁니다.
  - 원본에 있는 항목이 후보 견적에 통째로 없을 때. 항목은 `item` 이름으로 맞추므로 원본·후보가 같은 이름을 써야 합니다.
- **금액은 0 이상인 유한한 수만 받습니다.** NaN·무한대·음수는 입력 검사에서 거절합니다.
  항목 금액이 정상이어도 합·차·비율이 넘치면(예: `1e308 + 1e308`) 그 값은 `inf` 가 아니라 None("미제공")입니다.
- **입력 검사 (모두 422).** 한 클라우드에 같은 `item` 이 두 줄이면 거절합니다(합계가 부풀어 가짜 절감이 생김).
  후보 클라우드는 원본과 달라야 하고 서로 겹치면 안 됩니다(`/report` 는 `current.cloud`, `/estimate` 는 원본 `aws` 기준).
  `CostRequest` 크기는 주면 0 보다 크고 상한 이하(`schemas.MAX_*`), 모르면 None 그대로입니다.
  크기 제안(`Rightsizing`) 네 칸은 AI 설명과 같은 비용 판단 금지어(`schemas.JUDGEMENT_WORDS`)로 검사합니다. 측정값이 들어가야 하므로 숫자는 막지 않습니다.

## 비용 산출 (`estimate.py`, `POST /finops/estimate`)

크기(`CostRequest`)를 받아 항목 3개(앱 컴퓨팅 · DB 컴퓨팅 · DB 스토리지)의 월 비용을 가격 API 단가로 냅니다.
가격표는 프로세스 안에 6시간 보관합니다. 서버가 뜰 때 큰 가격표(AWS 리전 파일 · GCP 카탈로그)를 백그라운드로 받아 두므로
(약 20초, `ALPHA_FINOPS_WARMUP=0` 이면 끔) 그 뒤 첫 호출은 몇 초 안에 끝납니다. 월 환산은 730시간, 금액마다 `source` 에 API 이름과 조회 날짜를 남깁니다.

| 클라우드 | 가격 API | 고르는 단가 | 인증 |
|---|---|---|---|
| AWS (원본) | Price List Bulk API (리전별 공개 가격 파일). `AwsPrices(client=boto3 pricing)` 를 주면 Query API | Fargate Linux/x86 vCPU·GB 시간, RDS PostgreSQL Single-AZ 등급별 시간, gp3 GB-월 | 없음 (Query API 는 `pricing:GetProducts`) |
| GCP | Cloud Billing Catalog API | Cloud Run 인스턴스 기반 vCPU·GiB 초, Cloud SQL Enterprise 단일 영역 vCPU·RAM 시간, SSD GiB-월 | `ALPHA_GCP_PRICING_API_KEY` 또는 ADC |
| Azure | Retail Prices API | Container Apps 활성·유휴 vCPU·GiB 초와 요청 수수료, PostgreSQL Flexible 등급별 시간(크기 이상 가장 작은 등급), Storage GB-월 | 없음 |

가정과 한계 (화면·발표에서 같이 밝힐 것)
- 앱 인스턴스(복제본)는 `app_instances` 개(기본 1)입니다. `traffic` 을 안 주면 한 달 내내 켜져 있다고 셉니다.
- `traffic{idle_percent, rps}` 를 주면 배포 설정대로 켜진 시간을 셉니다. 값은 실측이 아니라 가정이라 결과 주의점에 그대로 적힙니다.
  - AWS Fargate: 서비스 태스크가 계속 떠 있어 그대로 내내.
  - GCP Cloud Run: `min_instance_count = 0` 이라 요청 없는 시간엔 0대. Terraform 이 `resources.limits` 를 적고 `cpu_idle` 은 안 적어서
    CPU 가 항상 할당되는 인스턴스 기반 과금이고 요청 수수료는 없습니다. 마지막 요청 뒤 줄어들기까지 기다리는 시간은 빠져 있습니다.
  - Azure Container Apps: 최소 복제본이 남아 요청 없는 시간은 유휴 요금, 요청 있는 시간은 활성 요금 + 백만 건당 요청 수수료.
- 무료 할당량·약정 할인·예약 할인은 빼지 않습니다 (결제 계정 단위라 이 앱 몫을 알 수 없음).
- 네트워크 송신, 백업, 로드밸런서, 고가용성(Multi-AZ·Regional)은 아직 넣지 않았습니다.
- 원본 DB 가 CPU 를 나눠 쓰는 저가형(RDS `db.t*`)이면 목적지도 저가형끼리, 원본 메모리 이상인 가장 작은 등급으로 셉니다
  (GCP 공유 코어 `db-f1-micro` 0.6GB · `db-g1-small` 1.7GB, Azure Burstable B1ms·B2s·B2ms). 전용 vCPU 와 비교하면 몇 배 비싸게 나오기 때문입니다.
- 그 밖에는 원본과 같은 vCPU·메모리로 셉니다 (Cloud SQL 전용 코어, Azure 크기 이상 가장 작은 등급). Cloud SQL 은 vCPU 당 메모리 범위 제한이 있어 실제 등급과 다를 수 있습니다.
- 단가가 0건이거나 애매하게 여러 건 걸리면 추측하지 않고 그 항목을 "미제공" 으로 둡니다.
- 원본 RDS 등급(`db_instance_class`)은 assessment 결과에 아직 없어서 호출하는 쪽이 넣어야 합니다. 없으면 AWS DB 는 미제공.

### 교차점 (`FinopsInput.crossover`, GCP 후보가 있을 때)

Cloud Run(요청 없는 시간 0대)과 Fargate(내내 켜짐)의 앱 컴퓨팅만 비교합니다. DB 는 트래픽과 상관없어 빼고 봅니다.
결과 문장은 `assumptions` 에 붙어 `report.notes` 로 나옵니다.

- `break_even_idle_percent`: 같은 크기·같은 대수일 때 유휴 비율이 이 값보다 크면 Cloud Run 이 작습니다.
  `1 - Fargate 월 / (Cloud Run 1대 한 달 내내 × 대수)` 이고 단가만으로 정해집니다.
- `break_even_rps`: 지금 유휴 비율에서 요청 있는 시간 평균 RPS 가 이 값 이하이면 Cloud Run 이 작습니다.
  요청 있는 시간에 필요한 Cloud Run 대수를 `ceil(RPS × 처리 시간 ÷ 동시 처리 80)` 으로 세고, Fargate 는 대수 고정으로 봅니다.
  처리 시간(`traffic.avg_latency_ms`)이 없으면, 1대만으로도 비싸면, Terraform 한도(`max_instance_count = 2`) 안에서 교차하지 않으면
  None 이고 `reason` 에 이유를 적습니다. 한도 밖은 짐작하지 않습니다.
- 동시 처리 80 은 Terraform 이 `max_instance_request_concurrency` 를 적지 않아 쓰는 Cloud Run 기본값입니다. 실측이 아닙니다.

## 입력 / 출력

| 계약 | 내용 |
|---|---|
| `FinopsInput` | `current`(지금 원본) · `candidates`(이전 후보) 각각 `CloudCost{cloud, region, lines[CostLine{item, monthly_usd, source}]}`, `rightsizing`, `period_days`, `assumptions`(계산 가정, 주의점에 실림) |
| `FinopsReport` | `savings[Saving{…, complete, missing}]`, `best`, `summary`, `notes`, `summary_source`("ai" 면 notes 끝에 AI 설명이 붙음) |

`FinopsInput` 은 `estimate.py` 가 만들고, 수동 입력이나 다른 계산(예: Infracost)도 같은 형식으로 넣을 수 있습니다.

## 파일

| 파일 | 역할 |
|---|---|
| `schemas.py` | 계약 |
| `service.py` | `run(inp, ctx)`: 절감 계산 → 규칙 문장·주의점 → (켜져 있으면) AI 설명 덧붙이기 |
| `ai.py` | Claude 설명 + 숫자·비용 판단 검사 |
| `estimate.py` | 가격 API 단가로 비용 항목 만들기 |
| `router.py` | `POST /finops/estimate`(가격 API) · `POST /finops/report`(이미 있는 비용, 수동 입력 포함) |

예시 입력: `tests/finops/fixtures/finops_input.sample.json` (금액은 테스트용 예시이고 실제 단가가 아닙니다)

## 확인

```
cd apps/backend
python -m pytest tests/finops -q
```
