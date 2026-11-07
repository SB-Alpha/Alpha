# finops (초안)

이전 후보별 월 비용을 비교해서 절감액을 계산하고, 승인자가 읽을 제안 문장을 붙이는 모듈입니다.

- 비용 계산(사용량·단가·Infracost 등): 박준서
- 절감 계산·설명 문장(규칙)·AI 주의점 선택: 이주호

## 원칙

- **숫자는 코드가 냅니다.** `savings`·`best` 는 항상 `service.py` 계산 결과입니다.
- **AI 는 주의점 코드만 고릅니다.** `ALPHA_AI_ENABLED=1` 일 때만 켜지고, 정해진 코드 4개(`ai.CaveatCode`) 중에서만 고릅니다.
  문장은 코드가 쓰고, 조건(`ai._CAVEATS`)이 맞는 코드의 고정 문장만 `notes` 뒤에 붙습니다. `summary`·`savings`·`best` 는 바꾸지 않습니다.
  모델에는 숫자와 플래그만 보내고, 호출이 실패하면 규칙 결과 그대로입니다.
- **모르는 값은 0 이 아니라 None("미제공").** 금액이 빠진 항목이 있는 후보는 절감액을 내지 않고 추천 후보에서도 뺍니다.
  일부만 더한 합계로 비교하면 실제보다 싸 보이기 때문입니다. 현재와 비교 항목이 다른 후보도 같습니다.
- **있을 수 없는 입력은 거절합니다.** 음수·Infinity·NaN·문자열 금액, 모르는 필드, 중복 후보, 현재와 같은 후보는 422 입니다.
  이 라우터의 422 본문에는 입력값을 싣지 않습니다.
- **절감액이 같은 후보가 여럿이면 `best` 는 None 입니다.** 코드가 임의로 고르지 않습니다.
- **크기 제안의 `evidence` 는 규칙 문장에 옮기지 않습니다.** 코드가 확인하지 않은 입력 문장이기 때문입니다.

## 입력 / 출력

| 계약 | 내용 |
|---|---|
| `FinopsInput` | `current`(지금 원본) · `candidates`(이전 후보) 각각 `CloudCost{cloud, region, lines[CostLine{item, monthly_usd, source}]}`, `rightsizing`, `period_days`. 모르는 필드는 422 |
| `FinopsReport` | `savings[Saving]`, `best`, `summary`, `notes`, `summary_source`. 타입은 "rules" / "ai" 이지만 AI 가 `summary` 를 쓰지 않으므로 값은 항상 "rules" |

`FinopsInput` 은 박준서님 계산 결과를 받을 자리라 형식이 확정되면 맞춥니다.

## 파일

| 파일 | 역할 |
|---|---|
| `schemas.py` | 계약과 입력 제한 (금액 범위, 길이·개수 상한, 모르는 필드·중복 후보 거절) |
| `service.py` | `run(inp, ctx)`: 절감 계산 → 규칙 문장 → (켜져 있으면) AI 가 고른 주의점 추가 |
| `ai.py` | Claude 로 주의점 코드 고르기 + 조건 확인 후 고정 문장 붙이기 |
| `router.py` | `POST /finops/report`. `main.py` 에 등록되어 있고, 아직 화면·워크플로 호출자는 없음. 422 는 `type`·`loc`·`msg` 만 돌려줌 |

예시 입력: `tests/finops/fixtures/finops_input.sample.json` (금액은 테스트용 예시이고 실제 단가가 아닙니다)

## 확인

```
cd apps/backend
python -m pytest tests/finops -q
```
