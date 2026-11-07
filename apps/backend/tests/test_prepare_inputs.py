"""Real contract, encrypted-store round trip, and no secret echo from terminal errors."""

import json
import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.core.secrets import put_secret, resolve
from app.features.db_migration import prepare_inputs as cli
from app.features.db_migration.cloud_lookup import FoundDb, FoundEndpoint
from app.features.db_migration.run_input import parse


AWS = {"aws_access_key_id": "test-key", "aws_secret_access_key": "never-echo-aws"}
GCP_KEY = {"type": "service_account", "project_id": "test-project", "client_email": "test@example.com",
           "private_key": "never-echo-private-key", "token_uri": "https://oauth2.googleapis.com/token"}
GCP = {"credentials_json": json.dumps(GCP_KEY)}
SOURCE = {"user": "alpha", "password": "never-echo-source"}
TARGET = {"user": "postgres", "password": "never-echo-target"}


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("ALPHA_SECRETS_KEY", Fernet.generate_key().decode())


def found(cloud, region):
    return FoundEndpoint(cloud, region, "scope", FoundDb("instance", "10.0.0.4", 5432, "16", False, True),
                         ("10.0.0.0/24",), {"vpc_name": "vpc"})


def bundle():
    return cli.build_bundle("mig-test", found("aws", "ap-northeast-2"), found("gcp", "asia-northeast3"),
                            SOURCE, TARGET, "alpha", AWS, GCP)


def test_contract_contains_real_supplied_passwords_and_explicit_unverified_status():
    result = bundle()
    request = result["run_input"]
    assert parse(request).target.db.password.get_secret_value() == TARGET["password"]
    assert set(request) == {"run_id", "source", "target"}
    assert request["source"]["db"] == SOURCE
    assert result["secret_values"]["cloud/gcp"] == GCP
    assert result["discovered"]["target"]["db"]["host"] == "10.0.0.4"
    assert result["verification"]["db_passwords"].startswith("not_checked")
    assert result["verification"]["migration"] == "not_started"


def test_save_does_not_register_and_sets_private_permissions(tmp_path):
    output = cli.save_bundle(bundle(), tmp_path / "export")
    assert output.is_absolute()
    assert output.stat().st_mode & 0o777 == 0o600
    assert output.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads(output.read_text())["run_input"]["source"]["db"] == SOURCE
    assert not (Path(os.environ["ALPHA_DATA_DIR"]) / "alpha.db").exists()
    assert cli.save_bundle(bundle(), output.parent) != output  # no accidental overwrites


def test_register_uses_backend_encryption_and_exact_001_names():
    cli.register_bundle(bundle())
    assert resolve("secret://cloud/aws") == AWS
    assert resolve("secret://cloud/gcp") == GCP
    assert resolve("secret://mig-test/source-db-admin") == SOURCE
    assert resolve("secret://mig-test/target-admin") == TARGET
    assert resolve("secret://mig-test/run-clouds")["target_region"] == "asia-northeast3"
    db = Path(os.environ["ALPHA_DATA_DIR"]) / "alpha.db"
    raw = db.read_bytes()
    for secret in (SOURCE["password"], TARGET["password"], GCP_KEY["private_key"], AWS["aws_secret_access_key"]):
        assert secret.encode() not in raw


def test_source_selection_only_uses_unambiguous_admin_ref():
    assert cli.stored_source(None) == (None, None)
    put_secret("mig-old/source-db-admin", SOURCE)
    assert cli.stored_source(None) == ("secret://mig-old/source-db-admin", SOURCE)
    put_secret("mig-other/source-db-admin", SOURCE)
    with pytest.raises(cli.PreparationError, match="여러"):
        cli.stored_source(None)
    assert cli.stored_source("mig-old")[1] == SOURCE


def test_saved_source_cloud_is_usable_without_registering_shared_credentials():
    put_secret("mig-old/source-cloud", AWS)
    assert cli.aws_credentials("secret://mig-old/source-db-admin", None) == AWS
    with pytest.raises(KeyError):
        resolve("secret://cloud/aws")


def test_temporary_aws_credentials_rejected():
    with pytest.raises(cli.PreparationError):
        cli.validate_aws({**AWS, "aws_session_token": "never-echo-token"})


@pytest.mark.parametrize("key", [[], {"project_id": "only-project"}, {**GCP_KEY, "token_uri": "https://evil.example"}])
def test_bad_key_does_not_expose_contents(tmp_path, key):
    path = tmp_path / "key.json"
    path.write_text(json.dumps(key))
    with pytest.raises(cli.PreparationError) as error:
        cli.read_gcp_key(path)
    assert "never-echo" not in str(error.value)


def test_key_is_preserved_as_full_string(tmp_path):
    path = tmp_path / "key.json"
    path.write_text(json.dumps(GCP_KEY, indent=2))
    assert cli.read_gcp_key(path) == {"credentials_json": path.read_text()}


def test_enter_copies_request_d_copies_store_values(monkeypatch):
    choices = iter(["", "d", "b", "q"])
    copied = []
    monkeypatch.setattr("builtins.input", lambda _: next(choices))
    monkeypatch.setattr(cli, "copy_json", copied.append)
    result = bundle()
    cli.clipboard_menu(result)
    assert copied == [result["run_input"], result["secret_values"], result]


def test_clipboard_passes_secrets_over_stdin_without_shell(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/tool")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    cli.copy_json(SOURCE)
    args, kwargs = calls[0]
    assert SOURCE["password"] not in str(args)
    assert json.loads(kwargs["input"]) == SOURCE
    assert "shell" not in kwargs
    assert kwargs["timeout"] == 10


def test_cli_provider_error_has_no_traceback_or_secret(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "stored_source", lambda _: ("secret://mig-old/source-db-admin", SOURCE))
    monkeypatch.setattr(cli, "aws_credentials", lambda *a: AWS)
    monkeypatch.setattr(cli, "read_gcp_key", lambda _: GCP)
    monkeypatch.setattr(cli, "ask_password", lambda _: TARGET["password"])
    def fail(*args):
        raise RuntimeError("never-echo-private-key")
    monkeypatch.setattr(cli, "lookup", fail)
    assert cli.main(["--gcp-key", "key.json", "--source-region", "ap-northeast-2", "--no-clipboard"]) == 1
    output = capsys.readouterr()
    assert "never-echo" not in output.out + output.err
    assert "Traceback" not in output.err


def test_cli_inputs_save_and_enter_without_register(monkeypatch, tmp_path, capsys):
    choices = iter(["ap-northeast-2", "key.json", "", "q"])
    copied = []
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: next(choices))
    monkeypatch.setattr(cli, "stored_source", lambda _: ("secret://mig-old/source-db-admin", SOURCE))
    monkeypatch.setattr(cli, "aws_credentials", lambda *a: AWS)
    monkeypatch.setattr(cli, "read_gcp_key", lambda _: GCP)
    monkeypatch.setattr(cli, "ask_password", lambda _: TARGET["password"])
    monkeypatch.setattr(cli, "gcp_region", lambda *a: "asia-northeast3")
    monkeypatch.setattr(cli, "lookup", lambda cloud, region, _: found(cloud, region))
    monkeypatch.setattr(cli, "copy_json", copied.append)
    assert cli.main(["--run-id", "mig-test", "--output-dir", str(tmp_path / "exports")]) == 0
    assert copied == [bundle()["run_input"]]
    assert "never-echo" not in capsys.readouterr().out
    assert not (Path(os.environ["ALPHA_DATA_DIR"]) / "alpha.db").exists()


@pytest.mark.parametrize("instances, expected", [
    ([], None),
    ([{"region": "asia-northeast3", "databaseVersion": "POSTGRES_16"}], "asia-northeast3"),
    ([{"region": "asia-northeast3", "databaseVersion": "POSTGRES_16"},
      {"region": "us-central1", "databaseVersion": "POSTGRES_16"}], None),
    ([{"region": "asia-northeast3", "databaseVersion": "MYSQL_8_0"}], None),
    ([{"region": "asia-northeast3", "databaseVersion": "POSTGRES_16", "instanceType": "READ_REPLICA_INSTANCE"}], None),
])
def test_gcp_region_is_queried_without_guessing_or_choosing_ambiguous_db(monkeypatch, instances, expected):
    from contextlib import nullcontext
    monkeypatch.setattr(cli.cloud_lookup, "_gcp_token", lambda _: "token")
    monkeypatch.setattr(cli.cloud_lookup, "_http", lambda: nullcontext(object()))
    monkeypatch.setattr(cli.cloud_lookup, "_gcp_pages", lambda *a: iter(instances))
    if expected:
        assert cli.gcp_region(GCP, None) == expected
    else:
        with pytest.raises(cli.PreparationError):
            cli.gcp_region(GCP, None)
