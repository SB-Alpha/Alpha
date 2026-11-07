"""Stage 9 through the HTTP client: six routes, selection and safe incomplete results."""

import copy
import json
from itertools import permutations

import httpx
import pytest
from cryptography.fernet import Fernet

from app.core.secrets import put_secret
from app.features.db_migration.catalog import (
    CatalogError, CatalogOptions, CatalogSelection, DatabaseSelectionRequired, discover_catalog,
)
from app.features.db_migration.client import AgentError, AgentSettings, CdcAgentClient
from app.features.db_migration.execution_model import build_execution_model

from .test_execution_model import REGIONS, inputs

PASSWORD = "catalog-password-marker"
TABLE = {"schema_name": "public", "name": "work_items", "primary_key": ["tenant_id", "id"],
         "kind": "table", "is_partition": False, "can_select": True}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALPHA_SECRETS_KEY", Fernet.generate_key().decode())

    def prepare(source="aws", target="gcp"):
        injected, found = inputs(source, target)
        model = build_execution_model(injected, found)
        for side in ("source", "target"):
            put_secret(getattr(model, side).credentials_ref.db_admin, {"user": "alpha", "password": PASSWORD})
        data = {"source": ["app"], "target": ["destination"], "calls": [], "responses": {}, "status": 200}

        def handler(request):
            assert request.method == "POST" and request.url.path == "/catalog"
            assert request.headers["CF-Access-Client-Id"] == "id"
            assert request.headers["CF-Access-Client-Secret"] == "access-secret"
            body = json.loads(request.content)
            side = "source" if body["host"] == model.source.db.host else "target"
            assert body["user"] == "alpha" and body["password"] == PASSWORD
            data["calls"].append((side, body))
            if data["status"] != 200:
                return httpx.Response(data["status"], json={"detail": PASSWORD})
            database = body["database"]
            result = {"database": database, "databases": data[side] if database is None else [],
                      "tables": [copy.deepcopy(TABLE)] if side == "source" and database else []}
            return httpx.Response(200, content=json.dumps(data["responses"].get((side, database), result)),
                                  headers={"Content-Type": "application/json"})

        client = CdcAgentClient(AgentSettings(url="https://agent.example", access_client_id="id",
                                            access_client_secret="access-secret"), transport=httpx.MockTransport(handler))
        return model, client, data

    return prepare


@pytest.mark.parametrize("source,target", tuple(permutations(REGIONS, 2)))
def test_six_routes_share_queries_and_keep_model_and_catalog_separate(setup, source, target):
    model, agent, data = setup(source, target)
    result = discover_catalog(model, agent=agent)
    assert result.execution == model
    assert result.source.database == "app" and result.target.database == "destination"
    assert result.source.tables[0].primary_key == ["tenant_id", "id"]
    assert result.target.tables == []
    assert "database" not in model.source.db.model_dump()
    assert [(s, q["database"]) for s, q in data["calls"]] == [
        ("source", None), ("target", None), ("source", "app"), ("target", "destination")]
    assert PASSWORD not in result.model_dump_json()


def test_multiple_databases_require_selection_even_when_postgres_is_present(setup):
    model, agent, data = setup()
    data["source"] = ["app", "postgres"]
    data["target"] = ["destination", "postgres"]
    with pytest.raises(DatabaseSelectionRequired) as exc:
        discover_catalog(model, agent=agent)
    assert exc.value.code == "database_selection_required"
    assert exc.value.candidates == {"source": ("app", "postgres"), "target": ("destination", "postgres")}
    assert len(data["calls"]) == 2  # No table read from a guessed DB.
    selected = discover_catalog(model, agent=agent, selection=CatalogSelection(source_database="app", target_database="destination"))
    assert selected.source.database == "app" and selected.target.database == "destination"


def test_explicit_postgres_selection_is_valid_and_never_replaced(setup):
    model, agent, data = setup()
    data["source"] = ["app", "postgres"]
    result = discover_catalog(model, agent=agent, selection=CatalogSelection(source_database="postgres"))
    assert result.source.database == "postgres"


@pytest.mark.parametrize("side", ["source", "target"])
def test_no_connectable_database_is_an_error(setup, side):
    model, agent, data = setup()
    data[side] = []
    with pytest.raises(CatalogError) as exc:
        discover_catalog(model, agent=agent)
    assert exc.value.code == "no_database"
    assert len(data["calls"]) == 2


def test_unknown_selection_is_rejected_before_reading_tables(setup):
    model, agent, data = setup()
    with pytest.raises(CatalogError) as exc:
        discover_catalog(model, agent=agent, selection=CatalogSelection(source_database=PASSWORD))
    assert exc.value.code == "invalid_database_selection" and PASSWORD not in str(exc.value)
    assert len(data["calls"]) == 2


@pytest.mark.parametrize("response", [None, [], {"database": None, "databases": ["app", "app"], "tables": []},
                                      {"database": None, "databases": ["app"], "tables": [TABLE]},
                                      {"database": None, "databases": ["app"], "tables": [], PASSWORD: PASSWORD}])
def test_malformed_database_lists_do_not_become_success(setup, response):
    model, agent, data = setup()
    data["responses"][("source", None)] = response
    with pytest.raises(CatalogError) as exc:
        discover_catalog(model, agent=agent)
    assert exc.value.code == "invalid_catalog_response" and PASSWORD not in str(exc.value)


@pytest.mark.parametrize("response", [
    {"database": "wrong", "databases": [], "tables": []},
    {"database": "app", "databases": [], "tables": [TABLE, TABLE]},
    {"database": "app", "databases": [], "tables": [{**TABLE, "can_select": PASSWORD}]},
])
def test_invalid_selected_database_responses_are_rejected(setup, response):
    model, agent, data = setup()
    data["responses"][("source", "app")] = response
    with pytest.raises(CatalogError) as exc:
        discover_catalog(model, agent=agent)
    assert exc.value.code == "invalid_catalog_response" and PASSWORD not in str(exc.value)


def test_table_facts_are_not_silently_filtered_or_invented(setup):
    model, agent, data = setup()
    table = {**TABLE, "name": "MixedCase", "primary_key": [], "can_select": False, "is_partition": True}
    data["responses"][("source", "app")] = {"database": "app", "databases": [], "tables": [table]}
    result = discover_catalog(model, agent=agent)
    assert result.source.tables[0].name == "MixedCase"
    assert not result.source.tables[0].primary_key and not result.source.tables[0].can_select
    assert result.source.tables[0].is_partition


def test_empty_source_does_not_produce_a_migration_catalog(setup):
    model, agent, data = setup()
    data["responses"][("source", "app")] = {"database": "app", "databases": [], "tables": []}
    with pytest.raises(CatalogError) as exc:
        discover_catalog(model, agent=agent)
    assert exc.value.code == "no_source_tables"


@pytest.mark.parametrize("status", [401, 403, 500, 502])
def test_agent_error_text_never_reveals_request_secrets(setup, status, caplog):
    model, agent, data = setup()
    data["status"] = status
    with pytest.raises(CatalogError) as exc:
        discover_catalog(model, agent=agent)
    assert exc.value.code == "catalog_unavailable" and PASSWORD not in str(exc.value) + caplog.text


def test_client_sanitizes_driver_errors_and_bad_json():
    def broken(request):
        raise httpx.ReadError(PASSWORD, request=request)

    for handler in (broken, lambda request: httpx.Response(200, text=PASSWORD),
                    lambda request: httpx.Response(500, json={"detail": PASSWORD})):
        agent = CdcAgentClient(AgentSettings(url="https://agent.example"), transport=httpx.MockTransport(handler))
        with pytest.raises(AgentError) as exc:
            agent.catalog({"password": PASSWORD})
        assert PASSWORD not in str(exc.value)


def test_simulation_does_not_fabricate_catalog_data():
    with pytest.raises(AgentError, match="real agent"):
        CdcAgentClient(AgentSettings(url="mock://simulation")).catalog({})


def test_stopped_discovery_never_hands_off_partial_catalog(setup):
    model, agent, data = setup()
    with pytest.raises(CatalogError) as exc:
        discover_catalog(model, agent=agent, stop_requested=lambda: len(data["calls"]) >= 3)
    assert exc.value.code == "stopped" and len(data["calls"]) == 3


def test_invalid_stored_account_stops_before_sending_a_request(setup):
    model, agent, data = setup()
    put_secret(model.source.credentials_ref.db_admin, {"user": "alpha", "password": f"{PASSWORD}\n"})
    with pytest.raises(CatalogError) as exc:
        discover_catalog(model, agent=agent)
    assert exc.value.code == "catalog_unavailable" and PASSWORD not in str(exc.value)
    assert not data["calls"]


def test_connection_policy_and_bootstrap_are_explicit_options(setup):
    model, agent, data = setup()
    discover_catalog(model, agent=agent, options=CatalogOptions(sslmode="verify-full", source_bootstrap_database="maintenance"))
    assert data["calls"][0][1]["bootstrap_database"] == "maintenance"
    assert all(q["sslmode"] == "verify-full" for _, q in data["calls"])
