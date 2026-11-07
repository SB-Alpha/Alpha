import socket
import subprocess
import threading

import pytest

from app.features.assessment import ssh_tunnel as st


class FakeProc:
    def __init__(self, cmd, listen=True, dead=False):
        self.cmd, self.pid, self.stdout, self.stderr = cmd, None, None, None
        self.waited = False
        self.rc = 255 if dead else None
        self.srv = None
        if listen and not dead:
            port = int(cmd[cmd.index("-L") + 1].split(":")[1])
            self.srv = socket.socket()
            self.srv.bind(("127.0.0.1", port))
            self.srv.listen()

    def poll(self):
        return self.rc

    def wait(self, timeout=None):
        self.waited = True
        if self.srv:
            self.srv.close()

    def terminate(self):
        pass

    def kill(self):
        pass


@pytest.fixture
def popen(monkeypatch):
    made = []

    def mk(**kw):
        def _p(cmd, **k):
            made.append(FakeProc(cmd, **kw))
            return made[-1]
        monkeypatch.setattr(subprocess, "Popen", _p)
        return made

    monkeypatch.setattr(st.shutil, "which", lambda n: "/usr/bin/ssh")
    monkeypatch.setattr(st, "_posix", lambda: False)
    monkeypatch.setattr(st, "_stop", lambda p: p.wait())
    return mk


def test_command_and_wait(popen, monkeypatch):
    monkeypatch.delenv("ALPHA_BASTION_KNOWN_HOSTS", raising=False)
    monkeypatch.setenv("ALPHA_BASTION_SSH_USER", "bob")
    made = popen()
    with st.forward("10.0.0.5", "db.internal", 5432, key_path="/k", proxy_command="nc %h %p") as (h, port):
        assert h == "127.0.0.1"
        cmd = made[0].cmd
        assert cmd[:2] == ["ssh", "-N"]
        assert f"127.0.0.1:{port}:db.internal:5432" in cmd
        for o in ("StrictHostKeyChecking=accept-new", "BatchMode=yes", "ExitOnForwardFailure=yes", "ProxyCommand=nc %h %p"):
            assert o in cmd
        assert any(c.startswith("ServerAliveInterval") for c in cmd)
        assert cmd[-1] == "bob@10.0.0.5"
    assert made[0].waited


def test_known_hosts_strict(popen, monkeypatch):
    monkeypatch.setenv("ALPHA_BASTION_KNOWN_HOSTS", "/etc/kh")
    made = popen()
    with st.forward("10.0.0.5", "db.internal", 5432):
        cmd = made[0].cmd
        assert "UserKnownHostsFile=/etc/kh" in cmd and "StrictHostKeyChecking=yes" in cmd
        assert "StrictHostKeyChecking=accept-new" not in cmd


def test_no_ssh(monkeypatch):
    monkeypatch.setattr(st.shutil, "which", lambda n: None)
    with pytest.raises(st.SshTunnelError, match="ssh"):
        with st.forward("h", "d", 1):
            pass


def test_early_exit(popen):
    made = popen(dead=True)
    with pytest.raises(st.SshTunnelError, match="먼저 종료"):
        with st.forward("h", "d", 1):
            pass
    assert made[0].waited


def test_timeout(popen):
    made = popen(listen=False)
    with pytest.raises(st.SshTunnelError, match="시간 초과"):
        with st.forward("h", "d", 1, ready_timeout=0.3):
            pass
    assert made[0].waited


@pytest.mark.parametrize("host", ["h; rm", "-oProxyCommand=x", "-h"])
def test_bad_host(host):  # 첫 글자가 '-' 면 ssh 옵션으로 읽힐 수 있어 거부
    with pytest.raises(st.SshTunnelError):
        with st.forward(host, "d", 1):
            pass
