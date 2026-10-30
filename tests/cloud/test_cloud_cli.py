import subprocess

import pytest

from variator.cloud import (
    AuthenticationError,
    CliError,
    NotFoundError,
    QuotaExceeded,
    cli_access_token,
    cli_job_logs,
)
from variator.cloud import cli as cli_module


class FakeRun:
    """Stands in for subprocess.run and records the commands."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "", exc: Exception | None = None):
        self.returncode, self.stdout, self.stderr, self.exc = returncode, stdout, stderr, exc
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        assert kwargs["capture_output"] and kwargs["text"] and kwargs["check"] is False
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)


def test_access_token_from_cli(monkeypatch):
    fake = FakeRun(stdout="eyJ.token.value\n")
    monkeypatch.setattr(cli_module.subprocess, "run", fake)
    assert cli_access_token() == "eyJ.token.value"
    assert fake.commands == [["nebius", "iam", "get-access-token"]]


def test_job_logs_command():
    fake = FakeRun(stdout="line 1\nVARIATOR_JOB_RESULT {}\n")
    assert cli_job_logs("aijob-1", tail=200, runner=fake).endswith("{}\n")
    assert fake.commands == [["nebius", "ai", "job", "logs", "aijob-1", "--tail", "200"]]


@pytest.mark.parametrize(
    ("code", "error"),
    [(7, AuthenticationError), (13, NotFoundError), (24, QuotaExceeded), (64, QuotaExceeded), (3, CliError)],
)
def test_exit_codes_are_mapped(code, error):
    with pytest.raises(error):
        cli_job_logs("aijob-1", runner=FakeRun(returncode=code, stderr="boom"))


def test_missing_cli_binary():
    with pytest.raises(CliError, match="not installed"):
        cli_access_token(runner=FakeRun(exc=FileNotFoundError("nebius")))


def test_cli_timeout():
    with pytest.raises(CliError, match="timed out"):
        cli_job_logs("j", runner=FakeRun(exc=subprocess.TimeoutExpired(["nebius"], 120)))
