"""원본 직접 입력 (source_kind="manual"). (이주호)

AWS 가 아닌 원본(GCP·Azure·온프레미스 등)은 아직 자동 조회가 없어서, 사람이 입력한 값으로
AWS 조회와 같은 형식의 조사 결과(DbAssessment · AppAssessment)를 만듭니다.

- 원본에서 확인한 값이 아니라서 gaps 에 '직접 입력' 이라고 남깁니다. 승인자가 계획 화면에서 보게 됩니다.
- 모르는 값은 지어내지 않고 None 으로 둡니다. 판정은 '확인 안 됨' 이 되고 목적지는 기본값을 씁니다.
- 환경 변수 중 비밀로 보이는 것은 app.py 와 같은 규칙으로 버립니다.
"""

from __future__ import annotations

from .app import has_secret_value, looks_secret
from .schemas import AppAssessment, AppInfo, AssessmentInput, DbAssessment, DbInfo, SourceDb

FARGATE_UNITS_PER_VCPU = 1024  # AppInfo.cpu_units 단위에 맞춤


def collect(inp: AssessmentInput) -> tuple[DbAssessment, AppAssessment]:
    m = inp.manual
    if m is None:  # AssessmentInput 검증에서 막히지만, 직접 부를 때를 위해
        raise ValueError("source_kind=manual 에는 manual 이 필요합니다")
    gaps = [f"원본({m.cloud}) 정보를 직접 입력받아 원본에서 확인하지 않음: 이전 전에 값 확인 필요"]

    env_vars: dict[str, str] = {}
    for name, value in m.app.env_vars.items():
        if looks_secret(name) or has_secret_value(value):
            gaps.append(f"직접 입력한 환경 변수 {name} 가 비밀로 보여 값을 넘기지 않음 (비밀 저장소로 옮겨야 함)")
            continue
        env_vars[name] = value

    if not m.app.image_uri:
        gaps.append("컨테이너 이미지 주소 미입력")
    app_facts = AppInfo(
        service_name=m.app.service_name,
        image_uri=m.app.image_uri,
        container_port=m.app.container_port,
        cpu_units=round(m.app.vcpu * FARGATE_UNITS_PER_VCPU) if m.app.vcpu else None,
        memory_mib=m.app.memory_mib,
        health_check_path=m.app.health_check_path,
        env_vars=env_vars,
    )

    d = m.db
    db_facts = DbInfo(
        engine="postgres",  # 지금 목적지·CDC 가 PostgreSQL 만 다룸 (팀 합의)
        publicly_accessible=d.publicly_accessible,
        engine_version=d.engine_version,
        database_name=d.database_name,
        username=m.app.db_user,
        allocated_storage_gb=d.storage_gb,
        tables=d.tables,
        logical_replication=d.logical_replication,
        memory_gib=d.memory_gib,
        vcpu=d.vcpu,
    )
    source_db = SourceDb(
        host=d.host,
        port=d.port,
        database=d.database_name,
        version=d.engine_version,
        publicly_accessible=d.publicly_accessible,
        logical_replication=d.logical_replication,
    )
    return (
        DbAssessment(facts=db_facts, source_db=source_db),
        AppAssessment(facts=app_facts, db_user=m.app.db_user, gaps=gaps),
    )
