"""RDS·EC2 조회부 (읽기 전용). db.py 가 가져다 씁니다.

원본 discovery/db_discovery.py 의 인스턴스·파라미터·네트워크 조회를 이식했습니다.
조회에 실패한 항목은 값을 지어내지 않고 None 으로 두며 gaps 에 문장만 남깁니다 (키·메시지 원문 금지).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from dataclasses import dataclass, field

from botocore.exceptions import BotoCoreError, ClientError

from .aws import AwsSession, error_code
from .schemas import DbInfo, SourceDb, SourceNetwork

_AWS_ERRORS = (BotoCoreError, ClientError)


@dataclass
class RdsResult:
    db: DbInfo = field(default_factory=DbInfo)
    source_db: SourceDb = field(default_factory=SourceDb)
    network: SourceNetwork = field(default_factory=SourceNetwork)
    resource_id: str | None = None  # DbiResourceId (ASM 시크릿 이름 폴백용)
    vpc_id: str | None = None
    db_name: str | None = None  # RDS 의 DBName (초기 DB 이름)
    gaps: list[str] = field(default_factory=list)


def _pick_instance(rds, instance_id: str | None, r: RdsResult) -> dict | None:
    if not instance_id:
        ids = [d["DBInstanceIdentifier"] for d in rds.describe_db_instances()["DBInstances"]]
        if not ids:
            r.gaps.append("RDS 인스턴스를 찾지 못함")
            return None
        if len(ids) > 1:
            r.gaps.append(f"RDS 인스턴스가 여러 개 — rds_instance_id 지정 필요: {', '.join(sorted(ids))}")
            return None
        instance_id = ids[0]
    instances = rds.describe_db_instances(DBInstanceIdentifier=instance_id)["DBInstances"]
    if not instances:
        r.gaps.append("RDS 인스턴스를 찾지 못함")
        return None
    return instances[0]


def _logical_replication(rds, groups: list[dict], r: RdsResult) -> bool | None:
    """rds.logical_replication 값. 재부팅 대기 중이면 아직 적용 전이라 False."""
    if not groups:
        r.gaps.append("파라미터 그룹을 찾지 못해 논리 복제 설정을 확인하지 못함")
        return None
    g = groups[0]
    value = None
    try:
        for page in rds.get_paginator("describe_db_parameters").paginate(DBParameterGroupName=g["DBParameterGroupName"]):
            for p in page["Parameters"]:
                if p["ParameterName"] == "rds.logical_replication":
                    value = p.get("ParameterValue")
    except _AWS_ERRORS as e:
        r.gaps.append(f"RDS 파라미터 그룹 조회 실패 ({error_code(e)})")
        return None
    if value == "1" and g.get("ParameterApplyStatus") == "pending-reboot":
        r.gaps.append("논리 복제 파라미터가 재부팅 대기 중(pending-reboot)")
        return False
    return value == "1"


def _collect_network(ec2, db: dict, peer_cidrs: Sequence[str], r: RdsResult) -> None:
    sg = db.get("DBSubnetGroup") or {}
    vpc_id = sg.get("VpcId")
    r.vpc_id = r.network.vpc_id = vpc_id
    sgs = db.get("VpcSecurityGroups") or []
    r.network.db_security_group_id = sgs[0]["VpcSecurityGroupId"] if sgs else None
    if not vpc_id:
        r.gaps.append("DB 서브넷 그룹에서 VPC를 찾지 못함")
        return
    try:
        vpc = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
        r.network.cidr = vpc["CidrBlock"]
        tables = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["RouteTables"]
    except _AWS_ERRORS as e:
        r.gaps.append(f"VPC 네트워크 조회 실패 ({error_code(e)})")
        return
    # 서브넷에 명시 연결이 없으면 VPC main 테이블을 쓴다
    main_rt = next((t["RouteTableId"] for t in tables for a in t["Associations"] if a.get("Main")), None)
    explicit = {a["SubnetId"]: t["RouteTableId"] for t in tables for a in t["Associations"] if a.get("SubnetId")}
    used: list[str] = []
    for s in sg.get("Subnets", []):
        rt = explicit.get(s["SubnetIdentifier"], main_rt)
        if rt and rt not in used:
            used.append(rt)
    r.network.route_table_ids = used
    cidrs = [a["CidrBlock"] for a in vpc.get("CidrBlockAssociationSet", []) if a["CidrBlockState"]["State"] == "associated"]
    for c in cidrs or [r.network.cidr]:
        for p in peer_cidrs:
            try:  # host bits 가 켜진 값(10.50.0.1/8)도 허용, 형식 오류는 gap
                overlap = ipaddress.ip_network(p, strict=False).overlaps(ipaddress.ip_network(c, strict=False))
            except ValueError:
                r.gaps.append(f"상대 대역 형식이 올바르지 않아 겹침을 확인하지 못함: {p}")
                continue
            if overlap:
                r.gaps.append(f"VPC CIDR({c})이 상대 대역({p})과 겹침")


def collect_rds(
    session: AwsSession, region: str, instance_id: str | None, peer_cidrs: Sequence[str] = ("10.50.0.0/16",)
) -> RdsResult:
    r = RdsResult()
    try:
        rds = session.client("rds", region_name=region)
        db = _pick_instance(rds, instance_id, r)
    except _AWS_ERRORS as e:
        r.gaps.append(f"RDS 조회 실패 ({error_code(e)})")
        return r
    if db is None:
        return r

    engine = db.get("Engine")
    ep = db.get("Endpoint") or {}
    r.resource_id = db.get("DbiResourceId")
    r.db_name = db.get("DBName")
    lr = _logical_replication(rds, db.get("DBParameterGroups") or [], r)
    r.db = DbInfo(
        engine=engine,
        publicly_accessible=db.get("PubliclyAccessible"),
        engine_version=db.get("EngineVersion"),
        instance_class=db.get("DBInstanceClass"),
        allocated_storage_gb=db.get("AllocatedStorage"),
        logical_replication=lr,
        ha_enabled=db.get("MultiAZ"), backup_retention_days=db.get("BackupRetentionPeriod"),
    )
    r.source_db = SourceDb(
        instance_id=db.get("DBInstanceIdentifier"), host=ep.get("Address"), port=ep.get("Port"),
        version=db.get("EngineVersion"), publicly_accessible=db.get("PubliclyAccessible"), logical_replication=lr,
    )
    try:
        ec2 = session.client("ec2", region_name=region)
        _collect_network(ec2, db, peer_cidrs, r)
    except _AWS_ERRORS as e:
        r.gaps.append(f"VPC 네트워크 조회 실패 ({error_code(e)})")
    return r
