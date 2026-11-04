"""원본 DB·네트워크 조회. (담당: 김준석)

채울 것 (계획서 1절):
- RDS: 주소·포트·DB 이름·버전·퍼블릭 액세스·인스턴스 클래스·디스크, 파라미터 rds.logical_replication
- PG 카탈로그 (inp.db_admin_ref 로 접속): wal_level, 테이블·행 수·PK
- 네트워크: VPC·CIDR·라우팅 테이블·DB 보안 그룹
반환: schemas.DbAssessment. 조회 실패한 값은 None 으로 두고 gaps 에 남깁니다.
"""

from __future__ import annotations

from .aws import AwsSession
from .schemas import AssessmentInput, DbAssessment


def collect(inp: AssessmentInput, session: AwsSession) -> DbAssessment:
    # 아직 구현 전. 가짜 값을 넣지 않고 "미구현" 을 그대로 알립니다 → DB 관련 check 는 unknown, 승인 불가.
    return DbAssessment(gaps=["DB·네트워크 조회 미구현 (db.py, 김준석)"])
