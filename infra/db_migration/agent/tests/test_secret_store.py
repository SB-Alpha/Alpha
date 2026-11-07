"""Secret validation, encryption and the per-node file, with Kafka replaced by a dict."""

import re
import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from cdc_agent import secret_store
from cdc_agent.config import Settings, read_properties
from cdc_agent.secret_store import CONNECT_KEYS, REQUIRED_KEYS, SecretsDisabled, SecretStore

VALUES = {key: f"value-{key}" for key in REQUIRED_KEYS}


@pytest.fixture
def topic(monkeypatch):
    records: dict = {}
    monkeypatch.setattr(secret_store, "read_topic", lambda settings, name: records)
    monkeypatch.setattr(SecretStore, "_send", lambda self, job_id, value: records.__setitem__(job_id, value))
    return records


def make(tmp_path: Path, key: str | None = None) -> SecretStore:
    settings = Settings("n1", "http://connect", "kafka", "_jobs", 1, tmp_path, tmp_path, None, None, 2, 10, 3,
                        secrets_key=key or Fernet.generate_key().decode(), agent_secrets_dir=tmp_path / "agent")
    settings.agent_secrets_dir.mkdir(exist_ok=True)
    return SecretStore(settings, store=None)


def read_both(folder: Path) -> dict:
    return {**read_properties(folder / "job01.properties"), **read_properties(folder / "agent" / "job01.properties")}


def test_put_encrypts_on_the_topic_and_writes_a_600_file(tmp_path, topic):
    make(tmp_path).put("job01", VALUES)
    path, agent_path = tmp_path / "job01.properties", tmp_path / "agent" / "job01.properties"

    assert b"value-source.password" not in topic["job01"]
    assert read_both(tmp_path) == VALUES
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(agent_path.stat().st_mode) == 0o600


def test_connect_file_holds_only_what_the_connectors_use(tmp_path, topic):
    make(tmp_path).put("job01", VALUES)

    assert read_properties(tmp_path / "job01.properties").keys() == CONNECT_KEYS
    assert "source.admin.password" not in (tmp_path / "job01.properties").read_text()
    assert "target.admin.password" in (tmp_path / "agent" / "job01.properties").read_text()


def test_connector_templates_use_only_the_connect_keys():
    templates = (Path(__file__).parents[2] / "connectors").glob("*.json")
    used = {m for t in templates for m in re.findall(r"\.properties:([a-z.]+)\}", t.read_text())}

    assert used == CONNECT_KEYS


def test_a_file_from_before_the_split_loses_the_other_accounts_on_sync(tmp_path, topic):
    store = make(tmp_path)
    store.put("job01", VALUES)
    (tmp_path / "job01.properties").write_text("source.admin.password=old\n")

    store.sync()

    assert read_properties(tmp_path / "job01.properties").keys() == CONNECT_KEYS


def test_other_nodes_get_the_file_on_sync(tmp_path, topic):
    key = Fernet.generate_key().decode()
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir(), second.mkdir()
    make(first, key).put("job01", VALUES)
    make(second, key).sync()

    assert read_both(second) == VALUES


def test_delete_removes_the_file_here_and_on_other_nodes(tmp_path, topic):
    key = Fernet.generate_key().decode()
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir(), second.mkdir()
    make(first, key).put("job01", VALUES)
    make(second, key).sync()
    make(first, key).delete("job01")

    assert topic["job01"] is None
    assert not (first / "job01.properties").exists()
    make(second, key).sync()
    assert not (second / "job01.properties").exists()
    assert not (second / "agent" / "job01.properties").exists()


def test_backslash_survives_for_both_readers(tmp_path, topic):
    make(tmp_path).put("job01", {**VALUES, "source.password": r"a\b=c#d"})
    path = tmp_path / "job01.properties"

    assert read_properties(path)["source.password"] == r"a\b=c#d"
    assert r"source.password=a\\b=c#d" in path.read_text()  # Java properties form


@pytest.mark.parametrize("change", [
    {"source.password": ""},
    {"source.password": "zz1\nzz2"},
    {"source.password": " zz3 "},
    {"source.password": 1},
    {"extra.key": "x"},
])
def test_bad_values_are_refused_without_echoing_them(tmp_path, topic, change):
    with pytest.raises(ValueError) as exc:
        make(tmp_path).put("job01", {**VALUES, **change})

    assert "zz" not in str(exc.value)
    assert topic == {}


def test_missing_key_is_named(tmp_path, topic):
    values = {k: v for k, v in VALUES.items() if k != "source.admin.password"}
    with pytest.raises(ValueError, match="source.admin.password"):
        make(tmp_path).put("job01", values)


def test_wrong_key_leaves_files_alone(tmp_path, topic):
    make(tmp_path).put("job01", VALUES)
    other = tmp_path / "other"
    other.mkdir()
    make(other).sync()

    assert not (other / "job01.properties").exists()


def test_bad_topic_key_cannot_write_outside_the_folder(tmp_path, topic):
    key = Fernet.generate_key().decode()
    topic["../evil"] = Fernet(key).encrypt(b"x=1\n")
    make(tmp_path, key).sync()

    assert list(tmp_path.parent.glob("evil*")) == []


def test_no_key_refuses_put_and_settings_repr_hides_the_key(tmp_path, topic):
    settings = Settings("n1", "c", "k", "_j", 1, tmp_path, tmp_path, None, None, 2, 10, 3)
    with pytest.raises(SecretsDisabled):
        SecretStore(settings, store=None).put("job01", VALUES)

    key = Fernet.generate_key().decode()
    shown = Settings("n1", "c", "k", "_j", 1, tmp_path, tmp_path, None, None, 2, 10, 3, secrets_key=key)
    assert key not in repr(shown)
