"""Read-only catalog SQL, credential handling and the HTTP contract without a database."""

from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cdc_agent import catalog, main

PASSWORD = "catalog-password-marker"
BODY = {"host": "db.private.example", "port": 5432, "user": "alpha", "password": PASSWORD}
TABLE = ("public", "work_items", "r", False, True, ["tenant_id", "id"])


@pytest.fixture
def database(monkeypatch):
    seen = {"connections": [], "sql": [], "rows": [("app",)], "closed": False}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            seen["closed"] = True

        def execute(self, sql):
            seen["sql"].append(sql)
            return SimpleNamespace(fetchall=lambda: seen["rows"])

    def connect(**kwargs):
        seen["connections"].append(kwargs)
        return Connection()

    monkeypatch.setattr(catalog.psycopg, "connect", connect)
    return seen


def test_database_listing_is_read_only_and_does_not_select_postgres(database):
    database["rows"] = [("app",), ("postgres",)]
    result = catalog.inspect_catalog(catalog.CatalogRequest(**BODY))
    assert result.database is None and result.databases == ["app", "postgres"] and result.tables == []
    connection = database["connections"][0]
    assert connection["dbname"] == "postgres" and connection["connect_timeout"] == 5
    assert connection["password"] == PASSWORD and connection["sslmode"] == "require"
    assert "default_transaction_read_only=on" in connection["options"]
    assert "statement_timeout=5000" in connection["options"]
    assert "lock_timeout=1000" in connection["options"]
    assert "datistemplate" in database["sql"][0] and "has_database_privilege" in database["sql"][0]
    assert database["closed"] and PASSWORD not in result.model_dump_json()


def test_selected_database_preserves_pk_order_and_inventory_facts(database):
    database["rows"] = [TABLE, ("sales", "orders", "p", False, False, []),
                        ("sales", "orders_2026", "r", True, True, ["id"])]
    result = catalog.inspect_catalog(catalog.CatalogRequest(**BODY, database="app"))
    assert database["connections"][0]["dbname"] == "app"
    assert result.database == "app" and result.databases == []
    assert result.tables[0].primary_key == ["tenant_id", "id"]
    assert result.tables[1].kind == "partitioned" and not result.tables[1].can_select
    assert result.tables[2].is_partition
    query = database["sql"][0]
    assert "k.position <= i.indnkeyatts" in query  # INCLUDE columns aren't primary key columns.
    assert "ORDER BY k.position" in query
    assert "information_schema" in query and "alpha_cdc" in query and "deptype = 'e'" in query
    assert "count(*)" not in query and "CREATE" not in query and "ALTER" not in query


def test_empty_target_is_a_valid_catalog(database):
    database["rows"] = []
    assert catalog.inspect_catalog(catalog.CatalogRequest(**BODY, database="destination")).tables == []


def test_database_and_bootstrap_names_are_parameters_not_sql(database):
    requested = "odd database'; SELECT 1; --"
    database["rows"] = []
    catalog.inspect_catalog(catalog.CatalogRequest(**BODY, database=requested))
    assert database["connections"][0]["dbname"] == requested
    assert requested not in database["sql"][0]
    catalog.inspect_catalog(catalog.CatalogRequest(**BODY, bootstrap_database="maintenance"))
    assert database["connections"][1]["dbname"] == "maintenance"


@pytest.mark.parametrize("table_mode,count", [(False, 1001), (True, 10001)])
def test_oversized_results_are_errors_instead_of_partial_success(database, table_mode, count):
    database["rows"] = [TABLE] * count if table_mode else [("app",)] * count
    with pytest.raises(catalog.CatalogError, match="not truncated"):
        catalog.inspect_catalog(catalog.CatalogRequest(**BODY, database="app" if table_mode else None))
    assert database["closed"]


def test_database_errors_never_echo_driver_text(monkeypatch, caplog):
    def broken(**kwargs):
        raise RuntimeError(PASSWORD)

    monkeypatch.setattr(catalog.psycopg, "connect", broken)
    query = catalog.CatalogRequest(**BODY)
    assert PASSWORD not in repr(query)
    with pytest.raises(catalog.CatalogError) as exc:
        catalog.inspect_catalog(query)
    assert PASSWORD not in str(exc.value) + caplog.text
    response = TestClient(main.app).post("/catalog", json=BODY)
    assert response.status_code == 502 and PASSWORD not in response.text + caplog.text


def test_agent_api_has_the_same_wire_shape_for_both_queries(database):
    client = TestClient(main.app)
    result = client.post("/catalog", json=BODY)
    assert result.status_code == 200
    assert result.json() == {"database": None, "databases": ["app"], "tables": []}
    database["rows"] = [TABLE]
    result = client.post("/catalog", json={**BODY, "database": "app"})
    assert result.status_code == 200 and result.json()["tables"][0]["schema_name"] == "public"
    assert PASSWORD not in result.text


@pytest.mark.parametrize("patch", [{"port": PASSWORD}, {"password": {"secret": PASSWORD}},
                                 {"sslmode": PASSWORD}, {PASSWORD: PASSWORD}, {"user": f"{PASSWORD}\n"},
                                 {"database": ""}, {"host": ""}])
def test_invalid_credentials_and_unknown_fields_are_not_echoed(database, patch):
    response = TestClient(main.app).post("/catalog", json={**BODY, **patch})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid catalog request"}
    assert PASSWORD not in response.text
    assert not database["connections"]


def test_non_object_and_malformed_json_are_not_echoed(database):
    client = TestClient(main.app)
    for response in (client.post("/catalog", json=[PASSWORD]), client.post("/catalog", content=PASSWORD)):
        assert response.status_code == 422 and PASSWORD not in response.text
    assert not database["connections"]


def test_backend_to_agent_catalog_contract(database, tmp_path, monkeypatch):
    import json
    import httpx
    from cryptography.fernet import Fernet

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4] / "apps/backend"))
    from app.core.secrets import put_secret
    from app.features.db_migration.catalog import discover_catalog
    from app.features.db_migration.client import AgentSettings, CdcAgentClient
    from app.features.db_migration.cloud_lookup import FoundDb, FoundEndpoint, FoundRoute
    from app.features.db_migration.execution_model import build_execution_model
    from app.features.db_migration.run_input import InjectedEndpoint, InjectedRun

    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALPHA_SECRETS_KEY", Fernet.generate_key().decode())
    source = InjectedEndpoint("aws", "ap-northeast-2", "secret://cloud/aws", "secret://mig-001/source-admin")
    target = InjectedEndpoint("azure", "koreacentral", "secret://cloud/azure", "secret://mig-001/target-admin")

    def found(endpoint, side):
        return FoundEndpoint(endpoint.cloud, endpoint.region, "scope",
                             FoundDb("instance", f"{side}.private", 5432, "16", False, None), ("10.0.0.0/24",), {})

    model = build_execution_model(InjectedRun("mig-001", source, target, "alpha"),
                                  FoundRoute(found(source, "source"), found(target, "target")))
    for endpoint in (source, target):
        put_secret(endpoint.db_admin_ref, {"user": "alpha", "password": PASSWORD})
    api = TestClient(main.app)

    def transport(request):
        body = json.loads(request.content)
        database["rows"] = [("app",)] if body["database"] is None else ([TABLE] if body["host"].startswith("source") else [])
        response = api.post(request.url.path, json=body)
        return httpx.Response(response.status_code, json=response.json())

    client = CdcAgentClient(AgentSettings(url="https://agent.example"), transport=httpx.MockTransport(transport))
    result = discover_catalog(model, agent=client)
    assert result.source.database == result.target.database == "app"
    assert result.source.tables[0].primary_key == ["tenant_id", "id"]
    assert result.target.tables == []
    assert len(database["connections"]) == 4
    assert PASSWORD not in result.model_dump_json()
