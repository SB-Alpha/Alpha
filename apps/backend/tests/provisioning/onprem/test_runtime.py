import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.features.provisioning.onprem.models import OnpremConfig
from app.features.provisioning.onprem.runtime import DockerRuntime
from app.features.provisioning.terraform import ProvisioningError


def config(tmp_path, **extra):
    return OnpremConfig(
        deployment_id="test-local",
        directory=tmp_path,
        docker_socket="/tmp/docker.sock",
        database="appdb",
        admin_ref="secret://admin",
        app_ref="secret://app",
        cloudflare_ref="secret://cf",
        worker_secret_ref="secret://worker",
        account_id="a" * 32,
        zone_id="b" * 32,
        tunnel_id="1" * 36,
        db_access_app_id="2" * 36,
        db_hostname="db.example.com",
        app_hostname="app.example.com",
        platform="linux/amd64",
        **extra,
    )


def test_was_and_db_have_no_published_ports_or_outbound_network(tmp_path):
    r = DockerRuntime(config(tmp_path))
    document = r.compose_document({"user": "admin"}, "image:fixed")
    assert document["networks"]["back"]["internal"] is True
    assert document["services"]["postgres"]["networks"] == ["back"]
    assert document["services"]["was"]["networks"] == ["back"]
    assert document["services"]["cloudflared"]["networks"] == ["back", "front"]
    for service in document["services"].values():
        assert "ports" not in service
    assert "POSTGRES_PASSWORD" not in document["services"]["postgres"]["environment"]


def test_secret_files_are_owner_only_and_cannot_be_overwritten(tmp_path):
    r = DockerRuntime(config(tmp_path))
    r.secret_file("admin", "secret-sentinel")
    path = tmp_path / "secrets" / "admin"
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ProvisioningError):
        r.secret_file("admin", "changed")
    assert path.read_text() == "secret-sentinel"


def test_command_errors_never_include_output_or_secrets(tmp_path):
    r = DockerRuntime(
        config(tmp_path),
        runner=lambda *a, **kw: SimpleNamespace(
            returncode=1, stdout="PRIVATE", stderr="PRIVATE"
        ),
    )
    with pytest.raises(ProvisioningError) as e:
        r.docker(["info"])
    assert "PRIVATE" not in str(e.value)


def test_image_archive_digest_and_loaded_id_are_checked(tmp_path, monkeypatch):
    import app.features.provisioning.onprem.runtime as module

    source = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/app@sha256:" + "a" * 64
    monkeypatch.setattr(module, "load_aws_credentials", lambda ref: {})
    monkeypatch.setattr(
        module,
        "aws_ecr_login",
        lambda *args: SimpleNamespace(
            registry="123456789012.dkr.ecr.ap-northeast-2.amazonaws.com",
            username="AWS",
            password="TOKEN",
        ),
    )
    r = DockerRuntime(config(tmp_path))
    calls = []
    folders = []

    def docker(args, **kwargs):
        calls.append(args)
        if "env" in kwargs:
            folder = Path(kwargs["env"]["DOCKER_CONFIG"])
            folders.append(folder)
            assert (folder / "config.json").stat().st_mode & 0o777 == 0o600
        if args[:2] == ["image", "inspect"]:
            return json.dumps(
                [
                    {
                        "Id": "sha256:" + "b" * 64,
                        "RepoDigests": [source],
                        "Os": "linux",
                        "Architecture": "amd64",
                    }
                ]
            )
        if args[:2] == ["compose", "version"]:
            return "2.40.0"
        if args[0] == "info":
            return json.dumps({"OSType": "linux", "Architecture": "x86_64"})
        if args[0] == "save":
            Path(args[2]).write_bytes(b"image-archive")
        return ""

    monkeypatch.setattr(r, "docker", docker)
    result = r.image(source, "secret://aws")
    assert result["archive_sha256"] == hashlib.sha256(b"image-archive").hexdigest()
    assert [a[0] for a in calls] == [
        "pull",
        "image",
        "info",
        "compose",
        "tag",
        "save",
        "load",
        "image",
    ]
    assert all(not f.exists() for f in folders)
    assert "TOKEN" not in json.dumps(result)


def test_loaded_image_mismatch_is_rejected(tmp_path, monkeypatch):
    import app.features.provisioning.onprem.runtime as module

    source = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/app@sha256:" + "a" * 64
    monkeypatch.setattr(module, "load_aws_credentials", lambda ref: {})
    monkeypatch.setattr(
        module,
        "aws_ecr_login",
        lambda *args: SimpleNamespace(
            registry="123456789012.dkr.ecr.ap-northeast-2.amazonaws.com",
            username="AWS",
            password="TOKEN",
        ),
    )
    r = DockerRuntime(config(tmp_path))
    seen = [0]

    def docker(args, **kwargs):
        if args[:2] == ["image", "inspect"]:
            seen[0] += 1
            return json.dumps(
                [
                    {
                        "Id": str(seen[0]),
                        "RepoDigests": [source],
                        "Os": "linux",
                        "Architecture": "amd64",
                    }
                ]
            )
        if args[:2] == ["compose", "version"]:
            return "2.40.0"
        if args[0] == "info":
            return json.dumps({"OSType": "linux", "Architecture": "x86_64"})
        if args[0] == "save":
            Path(args[2]).write_bytes(b"image-archive")
        return ""

    monkeypatch.setattr(r, "docker", docker)
    with pytest.raises(ProvisioningError, match="이미지 ID"):
        r.image(source, "secret://aws")


def test_stop_new_was_does_not_depend_on_restored_compose_file(tmp_path, monkeypatch):
    r = DockerRuntime(config(tmp_path))
    calls = []

    def docker(args, **kwargs):
        calls.append(args)
        return "abc123\n" if args[0] == "ps" else ""

    monkeypatch.setattr(r, "docker", docker)
    r.stop_was()
    assert not r.compose_path.exists()
    assert "label=com.docker.compose.service=was" in calls[0]
    assert calls[1] == ["stop", "--time", "10", "abc123"]
