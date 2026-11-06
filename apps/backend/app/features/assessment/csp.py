"""db.collect 의 CSP 수집기 계약. aws(db.py) · gcp(gcp_sql.py) · azure(azure_pg.py) 가 같은 모양을 돌려줍니다."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field

from .schemas import DbInfo, SourceDb, SourceNetwork

# 논리 복제 서버 플래그(파라미터) 이름. 켜짐 판정 값은 LOGICAL_ON.
LOGICAL_FLAG = {"aws": "rds.logical_replication", "gcp": "cloudsql.logical_decoding", "azure": "azure.replication_support"}
LOGICAL_ON = {"aws": "1", "gcp": "on", "azure": "logical"}


def logical_enabled(cloud: str, value: str | None) -> bool | None:
    """플래그 값 -> 켜짐 여부. 값을 못 읽었으면(None) 모름."""
    return None if value is None else value.strip().lower() == LOGICAL_ON[cloud]


@dataclass
class CspSource:
    """CSP 수집기 결과. 모르는 값은 None, 못 한 일은 gaps 에 한국어 문장(비밀 없음)으로.

    tunnel 이 None 이면 DB 접속 단계는 건너뜁니다(이유는 gaps 에 이미 있어야 함).
    tunnel() 은 컨텍스트 매니저로 (host, port) 를 줍니다. creds 는 {"user","password"}.
    """

    db: DbInfo = field(default_factory=DbInfo)  # logical_replication 은 LOGICAL_FLAG 로 채움
    source_db: SourceDb = field(default_factory=SourceDb)
    network: SourceNetwork = field(default_factory=SourceNetwork)
    db_name: str | None = None  # 인스턴스 생성 시 DB 이름(폴백)
    db_hint: str | None = None  # 앱 설정에서 찾은 DB 이름(보조)
    creds: dict | None = None
    tunnel: Callable[[], AbstractContextManager[tuple[str, int]]] | None = None
    gaps: list[str] = field(default_factory=list)
