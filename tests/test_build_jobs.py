"""Tests for the BuildJobManager (in-process image-build runner)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from fleetboot.server.build_jobs import (
    BuildAlreadyRunningError,
    BuildJobManager,
    JobState,
)


def _manager(repo: Path, makefile_body: str) -> BuildJobManager:
    # PHONY so make doesn't get confused by neighbouring directories with
    # the same name as the target.
    (repo / "Makefile").write_text(".PHONY: image\n" + makefile_body)
    return BuildJobManager(repo_root=repo)


def _wait_finished(manager: BuildJobManager, job_id: str, timeout: float = 5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        assert job is not None
        if job.state in (JobState.SUCCEEDED, JobState.FAILED):
            return job
        time.sleep(0.05)
    raise AssertionError("build did not finish in time")


def test_successful_build_records_succeeded(tmp_path: Path):
    manager = _manager(tmp_path, "image:\n\techo hello\n")
    job = manager.start(profile="default", architecture="amd64")
    finished = _wait_finished(manager, job.job_id)
    assert finished.state == JobState.SUCCEEDED
    assert finished.exit_code == 0
    assert any("hello" in line for line in manager.tail_log(job.job_id))


def test_failing_build_records_failed(tmp_path: Path):
    manager = _manager(tmp_path, "image:\n\texit 7\n")
    job = manager.start(profile="default", architecture="amd64")
    finished = _wait_finished(manager, job.job_id)
    assert finished.state == JobState.FAILED
    # make reports its own exit code (2) when a recipe step fails — not
    # the inner shell's exit code.
    assert finished.exit_code != 0


def test_only_one_build_at_a_time(tmp_path: Path):
    manager = _manager(tmp_path, "image:\n\tsleep 0.5\n")
    first = manager.start(profile="default", architecture="amd64")
    with pytest.raises(BuildAlreadyRunningError):
        manager.start(profile="default", architecture="amd64")
    _wait_finished(manager, first.job_id, timeout=5)
    # Now that the first finished, a new build is permitted.
    second = manager.start(profile="default", architecture="amd64")
    _wait_finished(manager, second.job_id, timeout=5)


def test_list_jobs_returns_newest_first(tmp_path: Path):
    manager = _manager(tmp_path, "image:\n\techo $(PROFILE)\n")
    a = manager.start(profile="default", architecture="amd64")
    _wait_finished(manager, a.job_id)
    b = manager.start(profile="default", architecture="amd64")
    _wait_finished(manager, b.job_id)
    listed = [j.job_id for j in manager.list_jobs()]
    # b started after a, so should sort earlier in the list (newest first).
    assert listed[0] == b.job_id
    assert listed[1] == a.job_id


def test_log_lines_persist_to_file(tmp_path: Path):
    manager = _manager(tmp_path, "image:\n\techo persist-me\n")
    job = manager.start(profile="default", architecture="amd64")
    finished = _wait_finished(manager, job.job_id)
    assert finished.log_path is not None
    log_text = Path(finished.log_path).read_text()
    assert "persist-me" in log_text


def test_manager_rejects_without_makefile(tmp_path: Path):
    with pytest.raises(RuntimeError):
        BuildJobManager(repo_root=tmp_path)
