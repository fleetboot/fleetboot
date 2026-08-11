"""Tests for the BuildJobManager (in-process image-build runner)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from fleetboot.server.build_jobs import (
    BuildAlreadyRunningError,
    BuildJobManager,
    JobState,
    compute_build_cascade,
)


def _manager(repo: Path, makefile_body: str) -> BuildJobManager:
    # PHONY so make doesn't get confused by neighbouring directories with
    # the same name as the target.
    (repo / "Makefile").write_text(".PHONY: image\n" + makefile_body)
    return BuildJobManager(repo_root=repo)


def _with_profiles(repo: Path, graph: dict[str, list[str]]) -> None:
    """Create image/profiles/<name>/ dirs, writing a `parent` file for
    each profile that declares one. `graph` maps profile -> parents."""
    profiles = repo / "image" / "profiles"
    for name, parents in graph.items():
        (profiles / name).mkdir(parents=True, exist_ok=True)
        if parents:
            (profiles / name / "parent").write_text(
                "\n".join(parents) + "\n"
            )


_TERMINAL = (JobState.SUCCEEDED, JobState.FAILED, JobState.SKIPPED)


def _wait_all(manager: BuildJobManager, jobs, timeout: float = 8):
    """Wait until every job reaches a terminal state (a cascade may end a
    descendant in SKIPPED, which _wait_finished doesn't treat as done)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = [manager.get(j.job_id) for j in jobs]
        if all(g is not None and g.state in _TERMINAL for g in got):
            return got
        time.sleep(0.05)
    raise AssertionError("cascade did not finish in time")


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


def test_on_success_callback_fires_only_when_build_succeeds(tmp_path: Path):
    """The manager only invokes on_success for SUCCEEDED jobs — a
    failed build cannot arm follow-up work like a fleet-wide reboot."""
    seen: list = []
    (tmp_path / "Makefile").write_text(".PHONY: image\nimage:\n\techo ok\n")
    manager = BuildJobManager(
        repo_root=tmp_path, on_success=lambda job: seen.append(job.job_id),
    )
    job = manager.start(profile="default", architecture="amd64")
    _wait_finished(manager, job.job_id)
    assert seen == [job.job_id]


def test_on_success_callback_skipped_when_build_fails(tmp_path: Path):
    seen: list = []
    (tmp_path / "Makefile").write_text(".PHONY: image\nimage:\n\texit 3\n")
    manager = BuildJobManager(
        repo_root=tmp_path, on_success=lambda job: seen.append(job.job_id),
    )
    job = manager.start(profile="default", architecture="amd64")
    _wait_finished(manager, job.job_id)
    assert seen == []


def test_on_success_receives_auto_reboot_flag(tmp_path: Path):
    """`auto_reboot` is per-invocation state stored on the job so the
    callback can decide whether to sweep the fleet."""
    captured: list = []
    (tmp_path / "Makefile").write_text(".PHONY: image\nimage:\n\techo ok\n")
    manager = BuildJobManager(
        repo_root=tmp_path,
        on_success=lambda job: captured.append(job.auto_reboot),
    )
    job = manager.start(
        profile="default", architecture="amd64", auto_reboot=True,
    )
    _wait_finished(manager, job.job_id)
    assert captured == [True]


def test_on_success_callback_errors_dont_fail_the_build(tmp_path: Path):
    """A crash in the fleet-wide reboot sweep must not retroactively
    downgrade a build that produced a valid artefact."""
    (tmp_path / "Makefile").write_text(".PHONY: image\nimage:\n\techo ok\n")

    def boom(_job):
        raise RuntimeError("pretend the registry blew up")

    manager = BuildJobManager(repo_root=tmp_path, on_success=boom)
    job = manager.start(profile="default", architecture="amd64")
    finished = _wait_finished(manager, job.job_id)
    assert finished.state == JobState.SUCCEEDED
    log = manager.tail_log(job.job_id)
    assert any("on_success callback failed" in line for line in log)


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


# ---- Build inheritance cascade -----------------------------------------


def test_compute_build_cascade_orders_parents_before_children(tmp_path: Path):
    """A diamond: base <- {left, right} <- join. Building base must return
    base first, its two children next, and the join (which inherits from
    both) only after both of its parents."""
    _with_profiles(tmp_path, {
        "base": [],
        "left": ["base"],
        "right": ["base"],
        "join": ["left", "right"],
        "unrelated": [],
    })
    order = compute_build_cascade(tmp_path / "image" / "profiles", "base")
    assert order[0] == "base"
    assert "unrelated" not in order
    assert set(order) == {"base", "left", "right", "join"}
    assert order.index("left") < order.index("join")
    assert order.index("right") < order.index("join")


def test_compute_build_cascade_leaf_is_just_itself(tmp_path: Path):
    _with_profiles(tmp_path, {"base": [], "leaf": ["base"]})
    assert compute_build_cascade(
        tmp_path / "image" / "profiles", "leaf"
    ) == ["leaf"]


def test_start_with_downstream_builds_root_then_every_descendant(tmp_path):
    manager = _manager(tmp_path, "image:\n\techo built $(PROFILE)\n")
    _with_profiles(tmp_path, {
        "base": [], "child": ["base"], "grandchild": ["child"],
    })
    jobs = manager.start_with_downstream(profile="base", architecture="amd64")
    assert [j.profile for j in jobs] == ["base", "child", "grandchild"]
    # Every job shares the cascade_root so the UI can group them.
    assert all(j.cascade_root == "base" for j in jobs)
    finished = _wait_all(manager, jobs)
    assert all(j.state == JobState.SUCCEEDED for j in finished)
    # Each ran its own make with its own PROFILE.
    for job in finished:
        assert any(
            f"built {job.profile}" in line
            for line in manager.tail_log(job.job_id)
        )


def test_start_with_downstream_leaf_profile_is_a_single_build(tmp_path):
    manager = _manager(tmp_path, "image:\n\techo ok\n")
    _with_profiles(tmp_path, {"base": [], "leaf": ["base"]})
    jobs = manager.start_with_downstream(profile="leaf", architecture="amd64")
    assert len(jobs) == 1
    # A single build is not labelled as part of a cascade.
    assert jobs[0].cascade_root is None
    finished = _wait_all(manager, jobs)
    assert finished[0].state == JobState.SUCCEEDED


def test_cascade_skips_descendants_when_root_build_fails(tmp_path):
    # Recipe fails for PROFILE=base only.
    manager = _manager(
        tmp_path, 'image:\n\t[ "$(PROFILE)" != "base" ]\n\techo ok\n',
    )
    _with_profiles(tmp_path, {"base": [], "child": ["base"]})
    jobs = manager.start_with_downstream(profile="base", architecture="amd64")
    finished = _wait_all(manager, jobs)
    by_profile = {j.profile: j for j in finished}
    assert by_profile["base"].state == JobState.FAILED
    # The descendant is SKIPPED (never built), not FAILED.
    assert by_profile["child"].state == JobState.SKIPPED
    assert by_profile["child"].exit_code is None


def test_cascade_continues_past_a_failing_descendant(tmp_path):
    # Root ok, childA fails, childB must still build.
    manager = _manager(
        tmp_path, 'image:\n\t[ "$(PROFILE)" != "childA" ]\n\techo ok\n',
    )
    _with_profiles(tmp_path, {
        "base": [], "childA": ["base"], "childB": ["base"],
    })
    jobs = manager.start_with_downstream(profile="base", architecture="amd64")
    finished = _wait_all(manager, jobs)
    by_profile = {j.profile: j for j in finished}
    assert by_profile["base"].state == JobState.SUCCEEDED
    assert by_profile["childA"].state == JobState.FAILED
    assert by_profile["childB"].state == JobState.SUCCEEDED


def test_cascade_fires_on_success_per_successful_profile(tmp_path):
    """auto_reboot's sweep runs per build with that build's own profile,
    so machines on a descendant profile get swept for the descendant's
    image."""
    swept: list = []
    (tmp_path / "Makefile").write_text(
        ".PHONY: image\nimage:\n\techo ok\n"
    )
    manager = BuildJobManager(
        repo_root=tmp_path,
        on_success=lambda job: swept.append(job.profile),
    )
    _with_profiles(tmp_path, {
        "base": [], "child": ["base"], "grandchild": ["child"],
    })
    jobs = manager.start_with_downstream(
        profile="base", architecture="amd64", auto_reboot=True,
    )
    _wait_all(manager, jobs)
    assert swept == ["base", "child", "grandchild"]


def test_cascade_holds_the_build_lock_for_the_whole_sequence(tmp_path):
    """An external submission while a cascade is mid-flight is rejected,
    exactly as it would be during a single build."""
    manager = _manager(tmp_path, "image:\n\tsleep 0.4\n\techo ok\n")
    _with_profiles(tmp_path, {"base": [], "child": ["base"]})
    jobs = manager.start_with_downstream(profile="base", architecture="amd64")
    with pytest.raises(BuildAlreadyRunningError):
        manager.start(profile="child", architecture="amd64")
    _wait_all(manager, jobs)
