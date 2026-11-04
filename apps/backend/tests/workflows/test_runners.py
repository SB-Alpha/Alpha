import json
from pathlib import Path

import pytest

from app.features.workflows.engine import Engine
from app.features.workflows.runners import RUNNERS, MissingStageOutput, run_planning
from app.features.workflows.schemas import (
    ManualStageInput,
    MigrationRequest,
    StageName,
    WorkflowState,
)
from app.features.workflows.store import RunStore

# planning fixture 는 PlanningInput 전체 모양이라, discovery 출력에 해당하는 inventory 만 씁니다.
INVENTORY = json.loads(
    (Path(__file__).parents[1] / "planning" / "fixtures" / "inventory.sample.json").read_text(encoding="utf-8")
)["inventory"]

REQUEST = MigrationRequest(
    github_repo_url="https://github.com/example/target-app",
    aws_region="ap-northeast-2",
    target_region="asia-northeast3",
)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return Engine(RunStore(tmp_path / "workflows.sqlite3"), RUNNERS)


def test_planning_runs_after_discovery_and_waits_for_plan_approval(engine):
    run_id = engine.advance(engine.create_run(REQUEST).run_id).run_id
    engine.submit_manual(run_id, StageName.DISCOVERY, ManualStageInput(output=INVENTORY, note="fixture"))

    run = engine.advance(run_id)

    assert run.state == WorkflowState.WAITING_APPROVAL
    assert run.waiting_gate == "plan"
    planning = run.stage(StageName.PLANNING)
    assert planning.state == WorkflowState.SUCCEEDED
    assert planning.manual is False
    plan = run.outputs["planning"]
    assert {"summary", "checks", "approvable", "blocking", "target_infra"} <= plan.keys()
    assert plan["target_infra"]["region"] == "asia-northeast3"


def test_planning_without_discovery_output_fails_loudly(engine):
    run = engine.create_run(REQUEST)

    with pytest.raises(MissingStageOutput):
        run_planning(run, ctx=None)  # type: ignore[arg-type]
