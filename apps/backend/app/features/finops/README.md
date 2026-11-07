# finops (초안)

이전 후보별 월 비용을 비교해서 절감액을 계산하고, 승인자가 읽을 제안 문장을 붙이는 모듈입니다.

- 비용 계산(사용량·단가·Infracost 등): 박준서
- 절감 계산·설명 문장(규칙 + AI): 이주호

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

## 입력 / 출력

| 계약 | 내용 |
|---|---|
| `FinopsInput` | `current`(지금 원본) · `candidates`(이전 후보) 각각 `CloudCost{cloud, region, lines[CostLine{item, monthly_usd, source}]}`, `rightsizing`, `period_days` |
| `FinopsReport` | `savings[Saving{…, complete, missing}]`, `best`, `summary`, `notes`, `summary_source`("ai" 면 notes 끝에 AI 설명이 붙음) |

`FinopsInput` 은 박준서님 계산 결과를 받을 자리라 형식이 확정되면 맞춥니다.

## 파일

| 파일 | 역할 |
|---|---|
| `schemas.py` | 계약 |
| `service.py` | `run(inp, ctx)`: 절감 계산 → 규칙 문장·주의점 → (켜져 있으면) AI 설명 덧붙이기 |
| `ai.py` | Claude 설명 + 숫자·비용 판단 검사 |
| `router.py` | `POST /finops/report`. 계약 확정 전이라 `main.py` 에 아직 등록하지 않음 |

예시 입력: `tests/finops/fixtures/finops_input.sample.json` (금액은 테스트용 예시이고 실제 단가가 아닙니다)

## 확인

```
cd apps/backend
python -m pytest tests/finops -q
```
