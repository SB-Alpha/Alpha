"""계획을 만드는 공통 경계(rules.build_target_infra)의 비밀 환경 변수 필터."""

from .test_plan import _ready_input, run


def test_plan_endpoint_filters_secret_env_like_the_collectors():
    # /assessment/plan 은 사실을 직접 받으므로 계획 생성 단계에서도 같은 비밀 필터를 거쳐야 함
    inp = _ready_input()
    inp.inventory.app.env_vars.update({"API_TOKEN": "tok-123", "DATABASE_URL": "postgres://u:p@h/db"})
    plan = run(inp)
    assert "API_TOKEN" not in plan.target_infra.env_vars and "DATABASE_URL" not in plan.target_infra.env_vars
    assert plan.target_infra.env_vars == {"LOG_LEVEL": "INFO"}
    assert sum(r.title == "비밀로 보이는 평문 환경 변수 제외" for r in plan.risks) == 2
    assert "tok-123" not in plan.model_dump_json()
