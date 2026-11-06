# finops (초안)

이전 후보별 월 비용을 비교해서 절감액을 계산하고, 승인자가 읽을 제안 문장을 붙이는 모듈입니다.

- 비용 계산(사용량·단가·Infracost 등): 박준서
- 절감 계산·설명 문장(규칙 + AI): 이주호

## 원칙

- **숫자는 코드가 냅니다.** `savings`·`best` 는 항상 `service.py` 계산 결과입니다.
- **AI 는 문장만 씁니다.** `ALPHA_AI_ENABLED=1` 일 때만 켜지고, 문장 속 숫자를 입력·계산 결과와 대조해서
  없는 숫자가 하나라도 있으면 규칙 문장으로 돌아갑니다 (`ai.unknown_numbers`). 호출이 실패해도 규칙 문장입니다.
- **모르는 값은 0 이 아니라 None("미제공").** 금액이 빠진 항목이 있는 후보는 절감액을 내지 않고 추천 후보에서도 뺍니다.
  일부만 더한 합계로 비교하면 실제보다 싸 보이기 때문입니다.

## 입력 / 출력

| 계약 | 내용 |
|---|---|
| `FinopsInput` | `current`(지금 원본) · `candidates`(이전 후보) 각각 `CloudCost{cloud, region, lines[CostLine{item, monthly_usd, source}]}`, `rightsizing`, `period_days` |
| `FinopsReport` | `savings[Saving]`, `best`, `summary`, `notes`, `summary_source`("rules" / "ai") |

`FinopsInput` 은 박준서님 계산 결과를 받을 자리라 형식이 확정되면 맞춥니다.

## 파일

| 파일 | 역할 |
|---|---|
| `schemas.py` | 계약 |
| `service.py` | `run(inp, ctx)`: 절감 계산 → 규칙 문장 → (켜져 있으면) AI 문장 |
| `ai.py` | Claude 로 문장 다듬기 + 숫자 검사 |
| `router.py` | `POST /finops/report`. 계약 확정 전이라 `main.py` 에 아직 등록하지 않음 |

예시 입력: `tests/finops/fixtures/finops_input.sample.json` (금액은 테스트용 예시이고 실제 단가가 아닙니다)

## 확인

```
cd apps/backend
python -m pytest tests/finops -q
```
