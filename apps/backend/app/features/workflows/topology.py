"""run 하나의 실제 구성도 (3D 토폴로지용). 요청·단계 출력·단계 상태·환경 설정 유무에서만 만듭니다.

모르는 값은 None 입니다. 고정된 예시 노드(Azure·온프렘 등)나 지연 수치는 만들지 않습니다.
노드 state 는 그 노드를 담당하는 단계의 WorkflowState 값입니다 (pending·running·succeeded 등).
DB 주소·사설 IP·Worker 주소는 resource 에 싣지 않고 인스턴스·서비스 이름만 씁니다.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .schemas import RunStatus, StageName
from .target_adapters import provider_of


class TopologyNode(BaseModel):
    id: str
    kind: str  # app | database | cdc_agent | vpn | edge
    label: str
    cloud: str | None = None
    region: str | None = None
    resource: str | None = None
    state: str | None = None  # 담당 단계의 WorkflowState


class TopologyEdge(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    from_: str = Field(alias="from")
    to: str
    kind: str  # app_db | replication | traffic
    via: str | None = None  # 거쳐 가는 노드 id (source_db→cdc_agent 는 "vpn")


class Topology(BaseModel):
    nodes: list[TopologyNode]
    edges: list[TopologyEdge]

# 노드를 담당하는 단계. 노드 state 는 이 단계의 상태입니다.
_OWNER_STAGE: dict[str, StageName] = {
    "source_app": StageName.PLANNING,
    "source_db": StageName.PLANNING,
    "cdc_agent": StageName.DB_MIGRATION,
    "vpn": StageName.DB_MIGRATION,
    "target_db": StageName.PROVISIONING,
    "target_app": StageName.APP_DEPLOY,
    "edge_worker": StageName.CUTOVER,
}


def _as_dict(value: Any) -> dict[str, Any]:
    """단계 출력은 저장소에서 dict 로 오지만, 모델 객체나 수동 입력(임의 모양)도 견딥니다."""
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _first(*values: Any) -> str | None:
    for value in values:
        text = _text(value)
        if text:
            return text
    return None


def _output(run: RunStatus, stage: StageName) -> dict[str, Any]:
    return _as_dict(run.outputs.get(stage.value))


def _stage_state(run: RunStatus, node_id: str) -> str | None:
    stage = _OWNER_STAGE[node_id]
    try:
        return run.stage(stage).state.value
    except StopIteration:
        return None


def _node(run: RunStatus, node_id: str, kind: str, label: str, **fields: str | None) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "label": label,
        "cloud": fields.get("cloud"),
        "region": fields.get("region"),
        "resource": fields.get("resource"),
        "state": _stage_state(run, node_id),
    }


def _source_fields(run: RunStatus) -> tuple[dict[str, str | None], dict[str, str | None]]:
    """원본 앱·DB. 조사 출력(planning.source)이 있으면 그것을, 없으면 요청 값을 씁니다."""
    req = run.request
    source = _as_dict(_output(run, StageName.PLANNING).get("source"))
    source_db = _as_dict(source.get("db"))
    cloud_src = req.cloud_source
    manual = req.manual

    if req.source_kind == "manual":
        cloud = _text(manual.cloud) if manual else None
        region = _first(source.get("region"), manual.region if manual else None)
        app_name = manual.app.service_name if manual else None
        db_name = None
    elif req.source_kind in ("gcp", "azure"):
        cloud = req.source_kind
        region = _first(source.get("region"), cloud_src.region if cloud_src else None)
        app_name = cloud_src.app_name if cloud_src else None
        db_name = cloud_src.db_instance if cloud_src else None
    else:
        cloud = "aws"
        region = _first(source.get("region"), req.aws_region)
        app_name = req.ecs_service
        db_name = req.rds_instance_id

    # 요청에서 서비스를 비워 assessment 가 고른 경우, 계획의 원래 이름(source_service_name)이 실제 원본 이름입니다.
    target_infra = _as_dict(_as_dict(_output(run, StageName.PLANNING).get("plan")).get("target_infra"))
    app = {"cloud": cloud, "region": region, "resource": _first(app_name, target_infra.get("source_service_name"))}
    db = {"cloud": cloud, "region": region, "resource": _first(source_db.get("instance_id"), db_name)}
    return app, db


def _target_fields(run: RunStatus) -> tuple[dict[str, str | None], dict[str, str | None]]:
    """목적지 앱·DB. provisioning.target·app_deploy 출력이 우선이고, 없으면 계획(plan.target_infra)·요청 값입니다."""
    req = run.request
    target = _as_dict(_output(run, StageName.PROVISIONING).get("target"))
    target_db = _as_dict(target.get("db"))
    plan = _as_dict(_output(run, StageName.PLANNING).get("plan"))
    target_infra = _as_dict(plan.get("target_infra"))
    deployed = _as_dict(_output(run, StageName.APP_DEPLOY).get("target"))

    cloud = _first(target.get("cloud"), deployed.get("cloud"), provider_of(req))
    region = _first(target.get("region"), deployed.get("region"), target_infra.get("region"), req.target_region)
    app = {"cloud": cloud, "region": region, "resource": _first(target_infra.get("service_name"))}
    db = {"cloud": cloud, "region": region, "resource": _first(target_db.get("instance"))}
    return app, db


def _env_flag(name: str) -> bool:
    return os.getenv(name, "0") in ("1", "true", "True")


def build_topology(run: RunStatus) -> dict[str, Any]:
    """노드 id 는 고정입니다. edge_worker 는 WORKER_URL 이 설정됐을 때만 있고, 없으면 그 노드와 간선을 빼고 돌려줍니다."""
    source_app, source_db = _source_fields(run)
    target_app, target_db = _target_fields(run)
    has_worker = bool(os.getenv("WORKER_URL"))
    vpn_label = "기존 VPN" if _env_flag("CDC_USE_EXISTING_VPN") else "VPN"

    nodes = [
        _node(run, "source_app", "app", "원본 앱", **source_app),
        _node(run, "source_db", "database", "원본 DB", **source_db),
        _node(run, "cdc_agent", "cdc_agent", "CDC 에이전트"),
        _node(run, "vpn", "vpn", vpn_label),
        _node(run, "target_db", "database", "목적지 DB", **target_db),
        _node(run, "target_app", "app", "목적지 앱", **target_app),
    ]
    edges = [
        {"id": "source_app-source_db", "from": "source_app", "to": "source_db", "kind": "app_db", "via": None},
        {"id": "source_db-cdc_agent", "from": "source_db", "to": "cdc_agent", "kind": "replication", "via": "vpn"},
        {"id": "cdc_agent-target_db", "from": "cdc_agent", "to": "target_db", "kind": "replication", "via": None},
        {"id": "target_app-target_db", "from": "target_app", "to": "target_db", "kind": "app_db", "via": None},
    ]
    if has_worker:
        # Worker 는 Cloudflare Worker 입니다 (infra/cloudflare/worker). 주소는 싣지 않습니다.
        nodes.append(_node(run, "edge_worker", "edge", "트래픽 게이트 (Worker)", cloud="cloudflare"))
        edges += [
            {"id": "edge_worker-source_app", "from": "edge_worker", "to": "source_app", "kind": "traffic", "via": None},
            {"id": "edge_worker-target_app", "from": "edge_worker", "to": "target_app", "kind": "traffic", "via": None},
        ]
    return {"nodes": nodes, "edges": edges}
