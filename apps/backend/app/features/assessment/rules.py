"""규칙 기반 검사와 매핑. AI 없이도 계획이 나와야 하므로 여기서 전부 결정합니다."""

from __future__ import annotations

import math
import re

from .app import PROVISIONING_INJECTED_ENV, has_secret_value, looks_secret
from .schemas import (
    InventoryForPlanning,
    PlanCheck,
    PlanStep,
    Risk,
    TargetInfra,
    TargetSelection,
)

TARGET_PG_MAJOR = 16  # 목적지 Cloud SQL PostgreSQL 16 고정 (MVP 범위)
# CDC 에이전트 사전 검사와 같은 기준 (infra/db_migration/agent/cdc_agent/db.py precheck_source:
# server_version_num >= 160000). 더 낮은 버전을 통과시키면 승인 뒤 CDC 단계에서 막힙니다.
MIN_SOURCE_PG_MAJOR = 16
# CDC 에이전트가 받는 테이블 이름 형식 (cdc_agent/models.py TABLE_PATTERN 과 같음). 소문자 schema.table 만
CDC_TABLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")

# 원본이 값을 안 줬을 때 쓰는 기본값. 쓰이면 risks 에 남깁니다.
# Cloud Run 인스턴스 한 개의 최대 크기. 원본 Fargate 태스크가 이보다 크면 같은 크기로 옮길 수 없음
CLOUD_RUN_MAX_VCPU = 8
CLOUD_RUN_MAX_MEMORY_MIB = 32768
DEFAULT_PORT = 8080
DEFAULT_HEALTH_PATH = "/health/ready"
DEFAULT_CPU = 1  # vCPU
DEFAULT_MEMORY = 512  # MiB


# RDS 인스턴스 클래스 → (메모리 GiB, vCPU). 클래스 이름은 "db.<계열>.<크기>" 이다.
# 2026-11-08 에 ap-northeast-2 에서 PostgreSQL 16 으로 주문 가능한 클래스 142개를 EC2 API(describe-instance-types)로 조회한 값이다.
# RDS 클래스는 같은 이름의 EC2 인스턴스와 메모리·vCPU 가 같다. 이 표에 없는 클래스는 메모리를 모르는 값(None)으로 두고,
# 목적지(provisioning)는 DB 등급을 따로 정하지 않고 기본값을 쓴다. 새 클래스를 넣을 때도 같은 API 로 확인한다.
_RDS_CLASS_SIZES: dict[str, dict[str, tuple[int | float, int]]] = {
    "t3": {"micro": (1, 2), "small": (2, 2), "medium": (4, 2), "large": (8, 2), "xlarge": (16, 4), "2xlarge": (32, 8)},
    "t4g": {"micro": (1, 2), "small": (2, 2), "medium": (4, 2), "large": (8, 2), "xlarge": (16, 4), "2xlarge": (32, 8)},
    "m5": {"large": (8, 2), "xlarge": (16, 4), "2xlarge": (32, 8), "4xlarge": (64, 16), "8xlarge": (128, 32), "12xlarge": (192, 48), "16xlarge": (256, 64), "24xlarge": (384, 96)},
    "m5d": {"large": (8, 2), "xlarge": (16, 4), "2xlarge": (32, 8), "4xlarge": (64, 16), "8xlarge": (128, 32), "12xlarge": (192, 48), "16xlarge": (256, 64), "24xlarge": (384, 96)},
    "m6g": {"large": (8, 2), "xlarge": (16, 4), "2xlarge": (32, 8), "4xlarge": (64, 16), "8xlarge": (128, 32), "12xlarge": (192, 48), "16xlarge": (256, 64)},
    "m6gd": {"large": (8, 2), "xlarge": (16, 4), "2xlarge": (32, 8), "4xlarge": (64, 16), "8xlarge": (128, 32), "12xlarge": (192, 48), "16xlarge": (256, 64)},
    "m6i": {"large": (8, 2), "xlarge": (16, 4), "2xlarge": (32, 8), "4xlarge": (64, 16), "8xlarge": (128, 32), "12xlarge": (192, 48), "16xlarge": (256, 64), "24xlarge": (384, 96), "32xlarge": (512, 128)},
    "m7g": {"large": (8, 2), "xlarge": (16, 4), "2xlarge": (32, 8), "4xlarge": (64, 16), "8xlarge": (128, 32), "12xlarge": (192, 48), "16xlarge": (256, 64)},
    "m7i": {"large": (8, 2), "xlarge": (16, 4), "2xlarge": (32, 8), "4xlarge": (64, 16), "8xlarge": (128, 32), "12xlarge": (192, 48), "16xlarge": (256, 64), "24xlarge": (384, 96), "48xlarge": (768, 192)},
    "m8g": {"large": (8, 2), "xlarge": (16, 4), "2xlarge": (32, 8), "4xlarge": (64, 16), "8xlarge": (128, 32), "12xlarge": (192, 48), "16xlarge": (256, 64), "24xlarge": (384, 96), "48xlarge": (768, 192)},
    "r5": {"large": (16, 2), "xlarge": (32, 4), "2xlarge": (64, 8), "4xlarge": (128, 16), "8xlarge": (256, 32), "12xlarge": (384, 48), "16xlarge": (512, 64), "24xlarge": (768, 96)},
    "r5d": {"large": (16, 2), "xlarge": (32, 4), "2xlarge": (64, 8), "4xlarge": (128, 16), "8xlarge": (256, 32), "12xlarge": (384, 48), "16xlarge": (512, 64), "24xlarge": (768, 96)},
    "r6g": {"large": (16, 2), "xlarge": (32, 4), "2xlarge": (64, 8), "4xlarge": (128, 16), "8xlarge": (256, 32), "12xlarge": (384, 48), "16xlarge": (512, 64)},
    "r6i": {"large": (16, 2), "xlarge": (32, 4), "2xlarge": (64, 8), "4xlarge": (128, 16), "8xlarge": (256, 32), "12xlarge": (384, 48), "16xlarge": (512, 64), "24xlarge": (768, 96), "32xlarge": (1024, 128)},
    "r7g": {"large": (16, 2), "xlarge": (32, 4), "2xlarge": (64, 8), "4xlarge": (128, 16), "8xlarge": (256, 32), "12xlarge": (384, 48), "16xlarge": (512, 64)},
    "r7i": {"large": (16, 2), "xlarge": (32, 4), "2xlarge": (64, 8), "4xlarge": (128, 16), "8xlarge": (256, 32), "12xlarge": (384, 48), "16xlarge": (512, 64), "24xlarge": (768, 96), "48xlarge": (1536, 192)},
    "r8g": {"large": (16, 2), "xlarge": (32, 4), "2xlarge": (64, 8), "4xlarge": (128, 16), "8xlarge": (256, 32), "12xlarge": (384, 48), "16xlarge": (512, 64), "24xlarge": (768, 96), "48xlarge": (1536, 192)},
    "r8gd": {"large": (16, 2), "xlarge": (32, 4), "2xlarge": (64, 8), "4xlarge": (128, 16), "8xlarge": (256, 32), "12xlarge": (384, 48), "16xlarge": (512, 64), "24xlarge": (768, 96), "48xlarge": (1536, 192)},
}
RDS_MEMORY_GIB = {
    f"db.{family}.{size}": memory for family, sizes in _RDS_CLASS_SIZES.items() for size, (memory, _) in sizes.items()
}
RDS_VCPU = {
    f"db.{family}.{size}": vcpu for family, sizes in _RDS_CLASS_SIZES.items() for size, (_, vcpu) in sizes.items()
}
CLOUD_SQL_MIN_DISK_GB = 10
# Cloud SQL 공유 코어 등급의 메모리 상한(GB). 이보다 큰 원본은 자동 매핑하지 않고 provisioning 기본값을 씁니다.
SHARED_CORE_TIER = ("db-g1-small", 1.7)


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
            detail=f"원본 {major} 은 CDC 가 지원하지 않습니다 (원본 PostgreSQL {MIN_SOURCE_PG_MAJOR} 이상 필요).",
        ))
    else:
        checks.append(PlanCheck(
            id="pg_version_compatible", title="PostgreSQL 버전 호환", result="pass",
            detail=f"원본 {db.engine_version} → 목적지 {TARGET_PG_MAJOR}",
        ))

    # 2. 논리 복제 가능. 실제로 조회한 wal_level 이 있으면 그 값이 우선입니다
    #    (rds.logical_replication=1 이어도 재시작 전이면 wal_level 은 아직 replica 일 수 있음).
    wal = (db.wal_level or "").strip().lower() or None
    if wal == "logical":
        checks.append(PlanCheck(
            id="logical_replication_enabled", title="논리 복제 설정", result="pass",
            detail="wal_level=logical 확인",
        ))
    elif wal is not None:
        pending = " rds.logical_replication 은 켜져 있지만 아직 적용되지 않았습니다." if db.logical_replication else ""
        checks.append(PlanCheck(
            id="logical_replication_enabled", title="논리 복제 설정", result="fail",
            detail=f"wal_level={wal} 입니다 (logical 필요).{pending} 켜려면 원본 DB 재시작이 필요합니다.",
        ))
    elif db.logical_replication is True:
        checks.append(PlanCheck(
            id="logical_replication_enabled", title="논리 복제 설정", result="pass",
            detail="rds.logical_replication=1 확인 (wal_level 은 조회하지 못함)",
        ))
    elif db.logical_replication is False:
        checks.append(PlanCheck(
            id="logical_replication_enabled", title="논리 복제 설정", result="fail",
            detail="논리 복제가 꺼져 있습니다 (rds.logical_replication=0). 켜려면 원본 DB 재시작이 필요합니다.",
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

    # 3-1. 테이블 이름이 CDC 가 받는 형식인지 (목록이 없으면 PK 검사가 이미 unknown 으로 막음)
    if db.tables:
        bad = [f"{t.schema_name}.{t.name}" for t in db.tables
               if not CDC_TABLE_PATTERN.match(f"{t.schema_name}.{t.name}")]
        checks.append(PlanCheck(
            id="table_names_supported", title="CDC 가 받는 테이블 이름", result="fail" if bad else "pass",
            detail=("소문자·숫자·_ 로만 된 schema.table 이 아닌 테이블: " + ", ".join(bad)) if bad
            else "모든 테이블 이름이 CDC 형식과 맞음",
        ))

    # 3-2. 복제 슬롯 여유. 조회한 값이 있을 때만 판정합니다 (직접 입력 원본은 값이 없을 수 있어 risks 로 남김)
    if db.max_replication_slots is not None and db.replication_slots_in_use is not None:
        free = db.max_replication_slots - db.replication_slots_in_use
        detail = f"사용 중 {db.replication_slots_in_use} / 최대 {db.max_replication_slots}"
        if free < 1:
            detail += ". CDC 가 슬롯을 만들 수 없습니다 (max_replication_slots 를 늘리거나 안 쓰는 슬롯 정리)"
        checks.append(PlanCheck(
            id="replication_slot_available", title="복제 슬롯 여유", result="pass" if free >= 1 else "fail",
            detail=detail,
        ))

    # 3-3. Cloud Run 으로 옮길 수 있는 설정인지. 태스크 정의를 본 경우에만 판정합니다 (직접 입력은 None)
    blockers = list(app.cloud_run_blockers or [])
    cpu = _cloud_run_cpu(app.cpu_units)
    if cpu is not None and cpu > CLOUD_RUN_MAX_VCPU:
        blockers.append(f"CPU {cpu} vCPU (Cloud Run 최대 {CLOUD_RUN_MAX_VCPU} vCPU)")
    if app.memory_mib is not None and app.memory_mib > CLOUD_RUN_MAX_MEMORY_MIB:
        blockers.append(f"메모리 {app.memory_mib} MiB (Cloud Run 최대 {CLOUD_RUN_MAX_MEMORY_MIB} MiB)")
    if app.cloud_run_blockers is not None or blockers:
        checks.append(PlanCheck(
            id="cloud_run_compatible", title="Cloud Run 호환", result="fail" if blockers else "pass",
            detail=("Cloud Run 으로 옮길 수 없는 설정: " + "; ".join(blockers)) if blockers
            else "옮길 수 없는 ECS 설정이 없음",
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


def _cloud_run_cpu(cpu_units: int | None) -> int | None:
    if cpu_units is None:
        return None
    return max(1, math.ceil(cpu_units / 1024))


def _cloud_run_memory(memory_mib: int | None) -> int | None:
    if memory_mib is None:
        return None
    return max(DEFAULT_MEMORY, memory_mib)


def _db_tier(gib: float | None) -> str | None:
    """원본 메모리 이상인 가장 작은 Cloud SQL 등급. 메모리를 모르거나 공유 코어보다 크면 None."""
    if gib is None:
        return None
    tier, tier_gb = SHARED_CORE_TIER
    return tier if gib <= tier_gb else None


def gcp_name(name: str) -> str:
    """GCP 자원 이름 규칙(소문자·숫자·하이픈, 문자로 시작, 49자 이하)에 맞춥니다. ECS 이름은 대문자·_ 를 허용함."""
    cleaned = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = "app-" + cleaned
    return cleaned[:49].rstrip("-")


def pinned_image(uri: str | None, digest: str | None) -> str | None:
    """digest 를 알면 `저장소@sha256:...` 로 고정합니다. provisioning 이 태그가 아니라 정확히 같은 이미지를 복사하도록."""
    if not uri or not digest or "@" in uri:
        return uri
    repo, _, last = uri.rpartition("/")
    name = last.split(":", 1)[0]
    return f"{repo}/{name}@{digest}" if repo else f"{name}@{digest}"


def image_repo_name(uri: str | None) -> str | None:
    """이미지 주소의 저장소 이름 (`.../target-app:tag` → `target-app`). 주소가 없으면 None."""
    if not uri:
        return None
    last = uri.rpartition("/")[2]
    name = last.split("@", 1)[0].split(":", 1)[0]
    return name or None


def _secret_name(service_name: str, env_name: str) -> str:
    return f"{service_name}-{env_name.lower().replace('_', '-')}"


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

    instance_class = (db.instance_class or "").lower()
    # 직접 입력은 메모리·vCPU 를 바로 받고, AWS 조회는 인스턴스 클래스 표로 구합니다
    db_memory_gib = db.memory_gib if db.memory_gib is not None else RDS_MEMORY_GIB.get(instance_class)
    db_vcpu = db.vcpu if db.vcpu is not None else RDS_VCPU.get(instance_class)
    db_tier = _db_tier(db_memory_gib)
    if db_tier is None:
        risks.append(Risk(
            level="low", title="DB 등급은 provisioning 기본값",
            detail=f"원본 DB 크기({db.instance_class or (f'{db_memory_gib:g}GiB' if db_memory_gib else '미제공')}) 는 "
                   "자동 매핑 범위 밖이라 "
                   "db_tier 를 넘기지 않습니다.",
        ))
    disk = max(CLOUD_SQL_MIN_DISK_GB, db.allocated_storage_gb) if db.allocated_storage_gb else None
    # ECS 서비스 이름은 "target-app-task-service-xxxx" 처럼 자동 생성 접미사가 붙어서,
    # 이미지 저장소 이름을 우선 씁니다. provisioning 의 GCP 자원 이름이 여기서 파생됨 (#27 리뷰)
    source_name = image_repo_name(app.image_uri) or app.service_name
    service_name = gcp_name(source_name)

    # 수집기(app.py·manual.py)가 이미 거르지만, /assessment/plan 처럼 사실을 직접 넣는 경로도 같은 규칙을 거칩니다
    plain_env: dict[str, str] = {}
    for k, v in app.env_vars.items():
        if k in PROVISIONING_INJECTED_ENV:
            continue
        if looks_secret(k) or has_secret_value(v):
            risks.append(Risk(level="high", title="비밀로 보이는 평문 환경 변수 제외",
                              detail=f"{k} 는 값이 비밀로 보여 target_infra 에 넣지 않았습니다. Secrets Manager 로 옮겨 주세요."))
            continue
        plain_env[k] = v

    infra = TargetInfra(
        region=target.region,
        service_name=service_name,
        source_service_name=source_name,
        source_image=pinned_image(app.image_uri, app.image_digest),
        container_port=fallback(app.container_port, DEFAULT_PORT, "컨테이너 포트"),
        cpu=fallback(_cloud_run_cpu(app.cpu_units), DEFAULT_CPU, "CPU"),
        memory=fallback(_cloud_run_memory(app.memory_mib), DEFAULT_MEMORY, "메모리(MiB)"),
        health_check_path=fallback(app.health_check_path, DEFAULT_HEALTH_PATH, "상태 확인 경로"),
        env_vars=plain_env,
        secret_refs={k: _secret_name(service_name, k)
                     for k in app.secret_refs if k not in PROVISIONING_INJECTED_ENV},
        db_version=str(TARGET_PG_MAJOR),
        db_name=db.database_name,
        db_user=db.username,
        db_tier=db_tier,
        db_memory_gib=db_memory_gib,
        db_vcpu=db_vcpu,
        db_disk_size_gb=disk,
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

    for note in inv.app.cloud_run_notes:
        risks.append(Risk(level="medium", title="Cloud Run 에서 확인할 ECS 설정", detail=note))

    moved = sorted(k for k in inv.app.secret_refs if k not in PROVISIONING_INJECTED_ENV)
    if moved:
        risks.append(Risk(
            level="high", title="비밀값 이전 필요",
            detail=f"비밀 {len(moved)}개({', '.join(moved)})의 값을 GCP Secret Manager 에 넣어야 합니다. "
                   "Terraform 은 빈 비밀만 만들고 Cloud Run 은 그 최신 값을 읽으므로, "
                   "값을 넣기 전에는 앱 배포가 실패합니다.",
        ))

    if inv.db.database_name is None:
        risks.append(Risk(level="medium", title="DB 이름 미제공", detail="목적지 DB 이름을 정할 수 없습니다."))

    if inv.db.max_replication_slots is None or inv.db.replication_slots_in_use is None:
        risks.append(Risk(level="medium", title="복제 슬롯 여유 미확인",
                          detail="원본에 빈 복제 슬롯이 없으면 CDC 시작 단계에서 실패합니다."))

    if inv.db.publicly_accessible is True:
        risks.append(Risk(
            level="high", title="원본 DB 퍼블릭 액세스 켜짐",
            detail="RDS 주소가 공인 IP 로 풀려 CDC 가 VPN 을 타지 못하고 접속에 실패합니다. "
                   "이전 전에 퍼블릭 액세스를 꺼야 합니다 (cdc 명세 2절, 대상 서비스 수정 T6).",
        ))

    risks.append(Risk(
        level="low", title="Cloud SQL 생성 시간",
        detail="Cloud SQL 생성은 오래 걸릴 수 있습니다. 실제 소요 시간은 provisioning 측정값을 따릅니다.",
    ))
    return risks


def image_registry_label(uri: str | None) -> str:
    """이미지 이동 단계에 보일 원본 저장소 이름. AWS ECR 주소면 "ECR", 아니면 주소의 호스트.

    첫 경로 조각에 `.`·`:` 이 있거나 `localhost` 일 때만 호스트로 봅니다 (docker 와 같은 규칙).
    `library/nginx`, `bitnami/postgresql` 처럼 계정 이름만 있으면 Docker Hub 입니다.
    """
    if not uri:
        return "원본 저장소(미제공)"
    first, sep, _ = uri.partition("/")
    if not sep or not ("." in first or ":" in first or first == "localhost"):
        return "Docker Hub"
    if ".dkr.ecr." in first:
        return "ECR"
    return first


def build_steps(image_uri: str | None = None) -> list[PlanStep]:
    return [
        PlanStep(order=1, module="workflows", action="계획 승인 ①", needs_approval=True),
        PlanStep(order=2, module="provisioning",
                 action=f"이미지 이동 {image_registry_label(image_uri)} → Artifact Registry"),
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
    line1 = (f"{infra.service_name} 를 GCP {infra.region} 의 Cloud Run({infra.cpu} vCPU, {infra.memory}MiB) "
             f"+ Cloud SQL PostgreSQL {infra.db_version} 로 옮깁니다.")
    line2 = f"필수 조건 {passed}/{len(checks)} 통과."
    if approvable:
        line3 = "승인하면 목적지를 만들고 CDC 동기화 후 전환 승인 ②에서 다시 멈춥니다."
    else:
        bad = [c.title for c in checks if c.result != "pass"]
        line3 = "승인 불가: " + ", ".join(bad) + " 확인 필요."
    return [line1, line2, line3]
