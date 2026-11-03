"""규칙 기반 검사와 매핑. AI 없이도 계획이 나와야 하므로 여기서 전부 결정합니다."""

from __future__ import annotations

import math

from .schemas import (
    InventoryForPlanning,
    PlanCheck,
    PlanStep,
    Risk,
    TargetInfra,
    TargetSelection,
)

TARGET_PG_MAJOR = 16  # 목적지 Cloud SQL PostgreSQL 16 고정 (MVP 범위)
MIN_SOURCE_PG_MAJOR = 10  # pgoutput 논리 복제 최소 버전

# 원본이 값을 안 줬을 때 쓰는 기본값. 쓰이면 risks 에 남깁니다.
DEFAULT_PORT = 8080
DEFAULT_HEALTH_PATH = "/health/ready"
DEFAULT_CPU = "1"
DEFAULT_MEMORY = "512Mi"


def _pg_major(version: str | None) -> int | None:
    if not version:
        return None
    head = version.strip().split(".")[0]
    return int(head) if head.isdigit() else None


def run_checks(inv: InventoryForPlanning) -> list[PlanCheck]:
    db, app = inv.db, inv.app
    checks: list[PlanCheck] = []

    # 1. PG 버전 호환
    major = _pg_major(db.engine_version)
    if db.engine is not None and db.engine.lower() not in ("postgres", "postgresql"):
        checks.append(PlanCheck(
            id="pg_version_compatible", title="PostgreSQL 버전 호환", result="fail",
            detail=f"원본 엔진이 {db.engine} 입니다. MVP 는 PostgreSQL 만 지원합니다.",
        ))
    elif major is None:
        checks.append(PlanCheck(
            id="pg_version_compatible", title="PostgreSQL 버전 호환", result="unknown",
            detail="원본 DB 버전 미제공",
        ))
    elif major > TARGET_PG_MAJOR:
        checks.append(PlanCheck(
            id="pg_version_compatible", title="PostgreSQL 버전 호환", result="fail",
            detail=f"원본 {major} > 목적지 {TARGET_PG_MAJOR}. 버전을 낮추는 이전은 지원하지 않습니다.",
        ))
    elif major < MIN_SOURCE_PG_MAJOR:
        checks.append(PlanCheck(
            id="pg_version_compatible", title="PostgreSQL 버전 호환", result="fail",
            detail=f"원본 {major} 은 논리 복제(pgoutput)를 지원하지 않습니다 (최소 {MIN_SOURCE_PG_MAJOR}).",
        ))
    else:
        checks.append(PlanCheck(
            id="pg_version_compatible", title="PostgreSQL 버전 호환", result="pass",
            detail=f"원본 {db.engine_version} → 목적지 {TARGET_PG_MAJOR}",
        ))

    # 2. 논리 복제 가능
    if db.logical_replication is True or (db.wal_level or "").lower() == "logical":
        checks.append(PlanCheck(
            id="logical_replication_enabled", title="논리 복제 설정", result="pass",
            detail="rds.logical_replication 또는 wal_level=logical 확인",
        ))
    elif db.logical_replication is False or db.wal_level is not None:
        checks.append(PlanCheck(
            id="logical_replication_enabled", title="논리 복제 설정", result="fail",
            detail=f"논리 복제가 꺼져 있습니다 (wal_level={db.wal_level or '미제공'}). "
                   "켜려면 원본 DB 재시작이 필요합니다.",
        ))
    else:
        checks.append(PlanCheck(
            id="logical_replication_enabled", title="논리 복제 설정", result="unknown",
            detail="논리 복제 설정 미제공",
        ))

    # 3. PK 존재
    if not db.tables:
        checks.append(PlanCheck(
            id="primary_keys_present", title="모든 테이블에 PK", result="unknown",
            detail="테이블 목록 미제공",
        ))
    else:
        no_pk = [f"{t.schema_name}.{t.name}" for t in db.tables if t.has_primary_key is False]
        unknown = [f"{t.schema_name}.{t.name}" for t in db.tables if t.has_primary_key is None]
        if no_pk:
            checks.append(PlanCheck(
                id="primary_keys_present", title="모든 테이블에 PK", result="fail",
                detail="PK 없는 테이블: " + ", ".join(no_pk),
            ))
        elif unknown:
            checks.append(PlanCheck(
                id="primary_keys_present", title="모든 테이블에 PK", result="unknown",
                detail="PK 여부 미제공: " + ", ".join(unknown),
            ))
        else:
            checks.append(PlanCheck(
                id="primary_keys_present", title="모든 테이블에 PK", result="pass",
                detail=f"{len(db.tables)}개 테이블 모두 PK 있음",
            ))

    # 4. 이미지 확보 가능 (주소가 있는지까지만. 실제 pull 은 provisioning 이 확인)
    if app.image_uri:
        checks.append(PlanCheck(
            id="image_available", title="컨테이너 이미지 확보", result="pass",
            detail=app.image_uri + (f"@{app.image_digest}" if app.image_digest else ""),
        ))
    else:
        checks.append(PlanCheck(
            id="image_available", title="컨테이너 이미지 확보", result="unknown",
            detail="원본 이미지 주소 미제공",
        ))

    return checks


def _cloud_run_cpu(cpu_units: int | None) -> str | None:
    if cpu_units is None:
        return None
    return str(max(1, math.ceil(cpu_units / 1024)))


def _cloud_run_memory(memory_mib: int | None) -> str | None:
    if memory_mib is None:
        return None
    return f"{max(512, memory_mib)}Mi"


def build_target_infra(inv: InventoryForPlanning, target: TargetSelection) -> tuple[TargetInfra, list[Risk]]:
    app, db = inv.app, inv.db
    risks: list[Risk] = []

    def fallback(value, default, what: str):
        if value is None:
            risks.append(Risk(
                level="medium", title=f"{what} 기본값 사용",
                detail=f"원본에서 {what} 을(를) 받지 못해 기본값 {default} 로 계획했습니다. 확인이 필요합니다.",
            ))
            return default
        return value

    infra = TargetInfra(
        region=target.region,
        service_name=app.service_name,
        source_image=app.image_uri,
        container_port=fallback(app.container_port, DEFAULT_PORT, "컨테이너 포트"),
        cpu=fallback(_cloud_run_cpu(app.cpu_units), DEFAULT_CPU, "CPU"),
        memory=fallback(_cloud_run_memory(app.memory_mib), DEFAULT_MEMORY, "메모리"),
        health_check_path=fallback(app.health_check_path, DEFAULT_HEALTH_PATH, "상태 확인 경로"),
        env_names=list(app.env_names),
        secret_refs=list(app.secret_refs),
        db_version=f"POSTGRES_{TARGET_PG_MAJOR}",
        db_name=db.database_name,
    )
    return infra, risks


def collect_risks(inv: InventoryForPlanning, checks: list[PlanCheck]) -> list[Risk]:
    risks: list[Risk] = []
    for c in checks:
        if c.result == "fail":
            risks.append(Risk(level="high", title=f"필수 조건 실패: {c.title}", detail=c.detail))
        elif c.result == "unknown":
            risks.append(Risk(level="medium", title=f"확인 안 됨: {c.title}", detail=c.detail))

    for gap in inv.gaps:
        risks.append(Risk(level="medium", title="원본 수집 누락", detail=gap))

    if inv.app.secret_refs:
        risks.append(Risk(
            level="medium", title="비밀값 이전 필요",
            detail=f"비밀 참조 {len(inv.app.secret_refs)}개를 GCP Secret Manager 에 다시 등록해야 합니다.",
        ))

    if inv.db.database_name is None:
        risks.append(Risk(level="medium", title="DB 이름 미제공", detail="목적지 DB 이름을 정할 수 없습니다."))

    risks.append(Risk(
        level="low", title="Cloud SQL 생성 시간",
        detail="Cloud SQL 생성은 오래 걸릴 수 있습니다. 실제 소요 시간은 provisioning 측정값을 따릅니다.",
    ))
    return risks


def build_steps() -> list[PlanStep]:
    return [
        PlanStep(order=1, module="workflows", action="계획 승인 ①", needs_approval=True),
        PlanStep(order=2, module="provisioning", action="이미지 이동 ECR → Artifact Registry"),
        PlanStep(order=3, module="provisioning",
                 action="terraform apply: VPC, Cloud SQL(논리 디코딩), Artifact Registry, Cloud Run, Secret Manager"),
        PlanStep(order=4, module="provisioning", action="Cloud Run 상태 확인 경로 성공 확인 → TargetEnv 반환"),
        PlanStep(order=5, module="db_migration", action="CDC 초기 복사 + 변경분 동기화, 행 수·데이터 대조"),
        PlanStep(order=6, module="workflows", action="전환 승인 ②", needs_approval=True),
        PlanStep(order=7, module="traffic",
                 action="Cloudflare 보류 → 최종 변경분 반영·검증 → 경로를 GCP 로 변경 → 보류 해제"),
    ]


def rule_summary(infra: TargetInfra, checks: list[PlanCheck], approvable: bool) -> list[str]:
    passed = sum(c.result == "pass" for c in checks)
    line1 = (f"{infra.service_name} 를 GCP {infra.region} 의 Cloud Run({infra.cpu} CPU, {infra.memory}) "
             f"+ Cloud SQL {infra.db_version} 로 옮깁니다.")
    line2 = f"필수 조건 {passed}/{len(checks)} 통과."
    if approvable:
        line3 = "승인하면 목적지를 만들고 CDC 동기화 후 전환 승인 ②에서 다시 멈춥니다."
    else:
        bad = [c.title for c in checks if c.result != "pass"]
        line3 = "승인 불가: " + ", ".join(bad) + " 확인 필요."
    return [line1, line2, line3]
