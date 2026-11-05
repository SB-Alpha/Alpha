"""TerraformCli 의 실제 프로세스 실행 부분을 시험한다.

진짜 terraform 대신 아주 작은 셸 스크립트를 "terraform" 으로 쓴다. 서브명령(init/plan/apply/show/output)에 따라
정해진 줄을 출력하고, 환경변수로 종료 코드나 느리게 끝나는 동작을 바꾼다.
"""

import time

import pytest

from app.features.provisioning.terraform import ProvisioningError, TerraformCli

SCRIPT = """#!/bin/sh
case "$1" in
  init) echo "Terraform has been successfully initialized!" ;;
  plan) echo "Plan: 1 to add, 0 to change, 0 to destroy."; exit ${FAKE_PLAN_EXIT:-2} ;;
  show) echo '{"resource_changes":[]}' ;;
  output)
    if [ -n "$FAKE_OUTPUT_FAIL" ]; then
      echo '{"admin":{"value":"TOP-SECRET-VALUE"}}'
      echo "boom: state backend unreachable" >&2
      exit 1
    fi
    echo '{"db_port":{"sensitive":false,"type":"number","value":5432}}' ;;
  apply)
    echo "a.b: Creating..."
    echo "a.b: Creation complete after 1s [id=x]"
    if [ -n "$FAKE_MARKER" ]; then trap 'echo got-sigint > "$FAKE_MARKER"; exit 130' INT; fi
    if [ -n "$FAKE_IGNORE_INT" ]; then trap '' INT; fi
    if [ -n "$FAKE_SLOW" ]; then sleep 30 & wait; fi
    exit ${FAKE_EXIT:-0} ;;
esac
"""


@pytest.fixture
def cli(tmp_path):
    binary = tmp_path / "fake-terraform"
    binary.write_text(SCRIPT)
    binary.chmod(0o755)
    return TerraformCli(tmp_path, binary=str(binary), timeout_s=20)


def test_plan_returns_the_detailed_exit_code(cli, tmp_path, monkeypatch):
    assert cli.plan(tmp_path / "v.json", tmp_path / "p") == 2  # 바뀔 것이 있음
    monkeypatch.setenv("FAKE_PLAN_EXIT", "0")
    assert cli.plan(tmp_path / "v.json", tmp_path / "p") == 0  # 바뀔 것이 없음


def test_plan_error_raises_with_the_last_lines(cli, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PLAN_EXIT", "1")
    with pytest.raises(ProvisioningError) as error:
        cli.plan(tmp_path / "v.json", tmp_path / "p")
    assert "종료 코드 1" in str(error.value)
    assert "Plan: 1 to add" in str(error.value)


def test_apply_streams_each_line_in_order_while_it_runs(cli, tmp_path):
    lines: list[str] = []
    cli.apply(tmp_path / "p", on_line=lines.append)
    assert lines == ["a.b: Creating...", "a.b: Creation complete after 1s [id=x]"]


def test_apply_failure_raises(cli, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT", "3")
    with pytest.raises(ProvisioningError, match="종료 코드 3"):
        cli.apply(tmp_path / "p")


def test_an_exception_in_the_line_handler_stops_terraform(cli, tmp_path, monkeypatch):
    # 중단 요청이 오면 ctx.report 가 예외를 내고, 그러면 실행 중인 terraform 도 멈춰야 한다
    monkeypatch.setenv("FAKE_SLOW", "1")

    def abort(line):
        if "Creation complete" in line:
            raise RuntimeError("aborted by operator")

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="aborted"):
        cli.apply(tmp_path / "p", on_line=abort)
    assert time.monotonic() - started < 10  # 30초 잠자는 프로세스를 기다리지 않고 끝나야 한다


def _abort_after_first_resource(line):
    if "Creation complete" in line:
        raise RuntimeError("aborted by operator")


def test_abort_sends_sigint_first_so_terraform_can_clean_up(cli, tmp_path, monkeypatch):
    # kill 로 바로 끝내면 terraform 이 상태 잠금을 풀 기회가 없다. SIGINT 를 받으면 스스로 정리하고 끝난다.
    marker = tmp_path / "sigint-marker"
    monkeypatch.setenv("FAKE_SLOW", "1")
    monkeypatch.setenv("FAKE_MARKER", str(marker))

    with pytest.raises(RuntimeError, match="aborted"):
        cli.apply(tmp_path / "p", on_line=_abort_after_first_resource)
    assert marker.read_text().strip() == "got-sigint"


def test_abort_force_kills_a_terraform_that_ignores_sigint_after_the_grace_period(tmp_path, monkeypatch):
    binary = tmp_path / "fake-terraform"
    binary.write_text(SCRIPT)
    binary.chmod(0o755)
    slow_cli = TerraformCli(tmp_path, binary=str(binary), timeout_s=20, stop_grace_s=1)
    monkeypatch.setenv("FAKE_SLOW", "1")
    monkeypatch.setenv("FAKE_IGNORE_INT", "1")

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="aborted"):
        slow_cli.apply(tmp_path / "p", on_line=_abort_after_first_resource)
    elapsed = time.monotonic() - started
    assert 0.8 <= elapsed < 10  # 유예 시간(1초)은 기다렸다가, 30초 잠자는 프로세스는 끝까지 기다리지 않는다


def test_output_and_show_are_parsed_as_json(cli, tmp_path):
    assert cli.output_json()["db_port"]["value"] == 5432
    assert cli.show_plan(tmp_path / "p") == {"resource_changes": []}


def test_failed_output_does_not_leak_stdout_into_the_error(cli, monkeypatch):
    monkeypatch.setenv("FAKE_OUTPUT_FAIL", "1")
    with pytest.raises(ProvisioningError) as error:
        cli.output_json()
    assert "boom" in str(error.value)
    assert "TOP-SECRET-VALUE" not in str(error.value)


def test_missing_binary_is_a_clear_error(tmp_path):
    with pytest.raises(ProvisioningError, match="찾을 수 없습니다"):
        TerraformCli(tmp_path, binary=str(tmp_path / "does-not-exist")).init()
