"""rds.py 테스트: 가짜 boto3 클라이언트로 조회 결과와 gaps 를 확인합니다."""

from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from app.features.assessment.rds import collect_rds

VPC, RTB, SG = "vpc-0example1", "rtb-0example1", "sg-0example1"
HOST = "db-example.abcdefghijkl.ap-northeast-2.rds.amazonaws.com"


def _inst(status="in-sync", groups=True):
    return {
        "DBInstanceIdentifier": "db-example", "DbiResourceId": "db-ABC", "Engine": "postgres",
        "EngineVersion": "16.15", "DBInstanceClass": "db.t3.micro", "AllocatedStorage": 20,
        "MultiAZ": True, "BackupRetentionPeriod": 7,
        "PubliclyAccessible": False, "Endpoint": {"Address": HOST, "Port": 5432},
        "VpcSecurityGroups": [{"VpcSecurityGroupId": SG}],
        "DBParameterGroups": [{"DBParameterGroupName": "pg", "ParameterApplyStatus": status}] if groups else [],
        "DBSubnetGroup": {"VpcId": VPC, "Subnets": [{"SubnetIdentifier": "subnet-1"}, {"SubnetIdentifier": "subnet-2"}]},
    }


def _session(instances=None, lr="1", status="in-sync", groups=True, cidr="172.31.0.0/16", explicit=False, denied=False):
    instances = [_inst(status, groups)] if instances is None else instances
    rds, ec2 = MagicMock(), MagicMock()
    rds.describe_db_instances.side_effect = lambda **kw: {
        "DBInstances": [i for i in instances if not kw or i["DBInstanceIdentifier"] == kw["DBInstanceIdentifier"]]
    }
    if denied:
        rds.describe_db_instances.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "DescribeDBInstances")
    rds.get_paginator.return_value.paginate.return_value = [
        {"Parameters": [{"ParameterName": "rds.logical_replication", "ParameterValue": lr}]}
    ]
    ec2.describe_vpcs.return_value = {"Vpcs": [{"CidrBlock": cidr, "CidrBlockAssociationSet": [
        {"CidrBlock": cidr, "CidrBlockState": {"State": "associated"}}]}]}
    assoc = [{"Main": True}] + ([{"SubnetId": "subnet-1"}] if explicit else [])
    rts = [{"RouteTableId": RTB, "Associations": assoc}]
    if explicit:
        rts = [{"RouteTableId": "rtb-main", "Associations": [{"Main": True}]},
               {"RouteTableId": "rtb-x", "Associations": [{"SubnetId": "subnet-1"}]}]
    ec2.describe_route_tables.return_value = {"RouteTables": rts}
    s = MagicMock()
    s.client.side_effect = lambda name, region_name=None: rds if name == "rds" else ec2
    return s


def test_normal():
    r = collect_rds(_session(), "ap-northeast-2", None)
    assert r.gaps == []
    assert r.db.engine == "postgres" and r.db.engine_version == "16.15"
    assert r.db.instance_class == "db.t3.micro" and r.db.allocated_storage_gb == 20
    assert r.db.logical_replication is True and r.db.database_name is None
    assert r.source_db.host == HOST and r.source_db.port == 5432 and r.source_db.database is None
    assert r.network.vpc_id == VPC and r.vpc_id == VPC and r.network.cidr == "172.31.0.0/16"
    assert r.network.route_table_ids == [RTB] and r.network.db_security_group_id == SG
    assert r.resource_id == "db-ABC"
    assert r.db.ha_enabled is True and r.db.backup_retention_days == 7 and r.network.allowed_cidrs == []


def test_ha_backup_absent():
    i = _inst()
    del i["MultiAZ"], i["BackupRetentionPeriod"]
    r = collect_rds(_session([i]), "ap-northeast-2", None)
    assert r.db.ha_enabled is None and r.db.backup_retention_days is None


def test_pending_reboot():
    r = collect_rds(_session(status="pending-reboot"), "r", None)
    assert r.db.logical_replication is False and r.source_db.logical_replication is False
    assert "논리 복제 파라미터가 재부팅 대기 중(pending-reboot)" in r.gaps


def test_no_parameter_group():
    r = collect_rds(_session(groups=False), "r", None)
    assert r.db.logical_replication is None and len(r.gaps) == 1


def test_zero_instances():
    r = collect_rds(_session(instances=[]), "r", None)
    assert r.gaps == ["RDS 인스턴스를 찾지 못함"] and r.db.engine is None and r.network.vpc_id is None


def test_many_instances_and_explicit():
    two = [_inst(), {**_inst(), "DBInstanceIdentifier": "a-other"}]
    r = collect_rds(_session(instances=two), "r", None)
    assert r.gaps == ["RDS 인스턴스가 여러 개 — rds_instance_id 지정 필요: a-other, db-example"]
    assert r.source_db.host is None
    assert collect_rds(_session(instances=two), "r", "db-example").gaps == []


def test_route_table_main_fallback_and_explicit():
    assert collect_rds(_session(), "r", None).network.route_table_ids == [RTB]
    assert collect_rds(_session(explicit=True), "r", None).network.route_table_ids == ["rtb-x", "rtb-main"]


def test_peer_overlap():
    r = collect_rds(_session(cidr="10.50.0.0/16"), "r", None)
    assert r.gaps == ["VPC CIDR(10.50.0.0/16)이 상대 대역(10.50.0.0/16)과 겹침"]


def test_access_denied():
    r = collect_rds(_session(denied=True), "r", None)
    assert r.gaps == ["RDS 조회 실패 (AccessDenied)"] and r.db.engine is None


def test_gaps_are_str():
    for s in (_session(), _session(denied=True), _session(instances=[]), _session(cidr="10.0.0.0/8")):
        assert all(isinstance(g, str) for g in collect_rds(s, "r", None).gaps)
