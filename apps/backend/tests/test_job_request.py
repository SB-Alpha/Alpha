"""10.1: which tables a job takes, and what the request carries."""

import pytest
from cryptography.fernet import Fernet

from app.core.secrets import put_secret, resolve
from app.features.db_migration.catalog import (
    CatalogExecution,
    CatalogTable,
    DatabaseCatalog,
)
from app.features.db_migration.execution_model import (
    ExecutionCredentials,
    ExecutionDatabase,
    ExecutionEndpoint,
    ExecutionModel,
    ExecutionNetwork,
)
from app.features.db_migration.job_request import (
    JobRequestError,
    build_request,
    ensure_account,
    ensure_account_refs,
    select_tables,
)

ADMIN_PASSWORD = "admin-password-marker"


def table(name="items", schema="public", **changes):
    values = {"schema_name": schema, "name": name, "primary_key": ["id"], "kind": "table",
              "is_partition": False, "can_select": True}
    return CatalogTable(**{**values, **changes})


def endpoint(side, cloud, host):
    return ExecutionEndpoint(
        cloud=cloud, region="region", scope_id="scope",
        db=ExecutionDatabase(instance="db", host=host, port=5432, version=None,
                             publicly_accessible=False, logical_replication=None),
        network=ExecutionNetwork(cidrs=("10.0.0.0/16",), provider_fields={}),
        credentials_ref=ExecutionCredentials(cloud=f"secret://cloud/{cloud}", db_admin=f"secret://mig-1/{side}-admin"),
    )


def catalog(*tables):
    execution = ExecutionModel(run_id="mig-1", source=endpoint("source", "gcp", "10.1.0.5"),
                               target=endpoint("target", "azure", "10.2.0.5"), target_app_user="app_new")
    return CatalogExecution(execution=execution, source=DatabaseCatalog(database="shop", tables=list(tables)),
                            target=DatabaseCatalog(database="shop_new", tables=[]))


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALPHA_SECRETS_KEY", Fernet.generate_key().decode())
    put_secret("mig-1/source-admin", {"user": "app_owner", "password": ADMIN_PASSWORD})


def test_all_user_tables_are_taken_and_the_agents_own_schema_is_not():
    assert select_tables(catalog(table("a"), table("b", "sales"), table("cutover_marker", "alpha_cdc"))) == [
        "public.a", "sales.b"]


@pytest.mark.parametrize(("bad", "code"), [
    (table("audit", primary_key=[]), "no_primary_key"),
    (table("audit", can_select=False), "table_not_readable"),
    (table("audit", kind="partitioned"), "partitioned_table"),
    (table("audit", is_partition=True), "partitioned_table"),
    (table("Audit"), "invalid_table_name"),
])
def test_a_table_that_cannot_move_stops_the_job_instead_of_being_dropped(bad, code):
    with pytest.raises(JobRequestError) as refused:
        select_tables(catalog(table(), bad))

    assert refused.value.code == code and bad.name in str(refused.value)


def test_a_long_list_of_refused_tables_is_shortened():
    bad = [table(f"t{i}", primary_key=[]) for i in range(8)]

    with pytest.raises(JobRequestError, match="and 3 more"):
        select_tables(catalog(*bad))


def test_only_the_agents_own_schema_means_nothing_to_move():
    with pytest.raises(JobRequestError) as refused:
        select_tables(catalog(table("cutover_marker", "alpha_cdc")))

    assert refused.value.code == "no_source_tables"


def test_request_takes_hosts_from_the_model_and_names_from_the_catalog():
    request = build_request("mig_1", catalog(table()))

    assert request["job_id"] == "mig_1"
    assert request["source"]["host"] == "10.1.0.5" and request["source"]["database"] == "shop"
    assert request["source"]["tables"] == ["public.items"] and request["source"]["app_role"] == "app_owner"
    assert request["target"]["host"] == "10.2.0.5" and request["target"]["database"] == "shop_new"
    assert request["target"]["app_role"] == "app_new"
    assert request["target"]["keep_ddl_user"] is False and request["target"]["keep_sink_user"] is False
    assert ADMIN_PASSWORD not in str(request)


def test_accounts_are_made_once_and_a_retry_keeps_their_passwords():
    execution = catalog(table()).execution

    refs = ensure_account_refs(execution)
    first = resolve(refs.target_ddl)["password"]

    assert ensure_account_refs(execution) == refs and resolve(refs.target_ddl)["password"] == first
    assert refs.source_admin == "secret://mig-1/source-admin" and refs.target_admin == "secret://mig-1/target-admin"
    assert refs.source_repl == "secret://mig-1/source-repl" and refs.target_sink == "secret://mig-1/target-sink-user"
    assert {resolve(refs.source_repl)["user"], resolve(refs.target_sink)["user"]} == {"alpha_cdc_repl", "alpha_sink"}


def test_a_saved_account_is_returned_as_it_is():
    put_secret("mig-1/given", {"user": "given", "password": "given-password"})

    assert ensure_account("secret://mig-1/given", "other") == {"user": "given", "password": "given-password"}
