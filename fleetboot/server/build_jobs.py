"""Asynchronous image-build job runner for the dashboard.

`POST /dashboard/builds` enqueues `make image PROFILE=<name> ARCH=<arch>`.
Concurrency is capped at one build at a time — debos is heavy and parallel
runs would just thrash the host. Subsequent submissions while a build is
running are rejected (HTTP 409).

The runner is in-process: jobs live in a thread-safe dict, log lines in a
bounded `deque` that the UI tails. Output is *also* persisted to a per-job
log file under ``logs/builds/`` so a long build's output isn't lost across
restarts.

No persistence of *job state* across restarts — that's deliberate for now;
all that survives is the on-disk artifacts (build/...) and the log files.
A future iteration can add a sqlite-backed history if useful.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    # A downstream build in a cascade that was not attempted because the
    # cascade's root build failed — the descendants share the root's
    # profile chain, so they would fail identically. Distinct from FAILED
    # (we never ran make) and from REJECTED (that's a concurrent submission).
    SKIPPED = "skipped"


@dataclass
class BuildJob:
    """One image-build invocation. Fields are filled as the job progresses."""

    job_id: str
    profile: str
    architecture: str
    state: JobState = JobState.QUEUED
    started_at: float = 0.0
    finished_at: Optional[float] = None
    exit_code: Optional[int] = None
    log_path: Optional[str] = None
    # When true, the manager's on_success callback is expected to sweep
    # the fleet and arm the soft-reboot signal on every machine running
    # this profile+arch. Stored on the job (not the manager) so it's
    # per-invocation and shows up in the log/history.
    auto_reboot: bool = False
    # Set by the manager once the on_success sweep runs — count of
    # machines that had the soft-reboot signal armed. Purely for UI.
    auto_reboot_armed: Optional[int] = None
    # When this job is part of an inheritance cascade (a build of a
    # profile that other profiles inherit from), this is the profile that
    # triggered it — the same value on every job in the cascade, root
    # included. None for a plain single-profile build. Lets the UI group
    # "these six builds came from rebuilding intel-graphics".
    cascade_root: Optional[str] = None
    # Bounded in-memory ring of recent lines, for the dashboard's live tail.
    # The full log lives at ``log_path``.
    recent_lines: deque[str] = field(
        default_factory=lambda: deque(maxlen=2000)
    )


class BuildAlreadyRunningError(RuntimeError):
    """Raised when a new build is submitted while one is already running."""


class BuildJobManager:
    """In-process build job manager. Thread-safe."""

    def __init__(
        self,
        *,
        repo_root: Path,
        log_dir: Optional[Path] = None,
        make_path: str = "make",
        on_success: Optional[Callable[["BuildJob"], None]] = None,
    ) -> None:
        if not (repo_root / "Makefile").is_file():
            raise RuntimeError(
                f"repo_root {repo_root} has no Makefile; "
                "BuildJobManager needs a fleetboot repo checkout"
            )
        self._repo_root = repo_root
        self._log_dir = log_dir or (repo_root / "build" / "logs")
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._make_path = make_path
        self._jobs: dict[str, BuildJob] = {}
        self._jobs_lock = threading.Lock()
        self._running_lock = threading.Lock()
        # Called with the BuildJob when it transitions to SUCCEEDED.
        # The dashboard wire-up uses this to fan-out a fleet-wide reboot
        # when the job was started with auto_reboot=True.
        self._on_success = on_success

    # ---- public API -----------------------------------------------------

    def is_running(self) -> bool:
        return self._running_lock.locked()

    def list_jobs(self) -> list[BuildJob]:
        with self._jobs_lock:
            return sorted(
                self._jobs.values(),
                key=lambda j: j.started_at or 0,
                reverse=True,
            )

    def get(self, job_id: str) -> Optional[BuildJob]:
        with self._jobs_lock:
            return self._jobs.get(job_id)

    def tail_log(self, job_id: str, n: int = 200) -> list[str]:
        job = self.get(job_id)
        if job is None:
            return []
        # Recent in-memory ring is the fastest source. If the build is
        # complete we still have it intact until manager restart.
        return list(job.recent_lines)[-n:]

    def _new_job(
        self,
        *,
        profile: str,
        architecture: str,
        auto_reboot: bool,
        cascade_root: Optional[str] = None,
    ) -> BuildJob:
        job_id = _make_id()
        job = BuildJob(
            job_id=job_id,
            profile=profile,
            architecture=architecture,
            state=JobState.QUEUED,
            log_path=str(self._log_dir / f"{job_id}.log"),
            auto_reboot=auto_reboot,
            cascade_root=cascade_root,
        )
        with self._jobs_lock:
            self._jobs[job_id] = job
        return job

    def start(
        self,
        *,
        profile: str,
        architecture: str = "amd64",
        auto_reboot: bool = False,
    ) -> BuildJob:
        """Spawn a single build. Raises if one is already running.

        `auto_reboot` — if true, on successful build the manager's
        on_success callback fires, which the app wires up to arm the
        soft-reboot signal on every machine running this profile+arch.
        """
        if not self._running_lock.acquire(blocking=False):
            raise BuildAlreadyRunningError(
                "another build is already running"
            )
        job = self._new_job(
            profile=profile, architecture=architecture,
            auto_reboot=auto_reboot,
        )
        thread = threading.Thread(
            target=self._run_single, args=(job,), daemon=True,
            name=f"build-{job.job_id}",
        )
        thread.start()
        return job

    def start_with_downstream(
        self,
        *,
        profile: str,
        architecture: str = "amd64",
        auto_reboot: bool = False,
    ) -> list[BuildJob]:
        """Rebuild `profile` and every profile that inherits from it.

        Returns the ordered list of jobs (the root first, then each
        descendant). They run sequentially under a single hold of the
        build lock — debos is heavy, and holding the lock for the whole
        cascade means an external submission still gets a clean "already
        running" rather than interleaving. If the root build fails, the
        descendants are marked SKIPPED rather than run: they embed the
        root's (now broken) profile chain and would fail identically.

        For a profile with no descendants this is exactly one build, same
        as `start`.
        """
        if not self._running_lock.acquire(blocking=False):
            raise BuildAlreadyRunningError(
                "another build is already running"
            )
        profiles_root = self._repo_root / "image" / "profiles"
        cascade = compute_build_cascade(profiles_root, profile)
        cascade_root = profile if len(cascade) > 1 else None
        jobs = [
            self._new_job(
                profile=name, architecture=architecture,
                auto_reboot=auto_reboot, cascade_root=cascade_root,
            )
            for name in cascade
        ]
        thread = threading.Thread(
            target=self._run_cascade, args=(jobs,), daemon=True,
            name=f"build-cascade-{jobs[0].job_id}",
        )
        thread.start()
        return jobs

    # ---- internals ------------------------------------------------------

    def _run_single(self, job: BuildJob) -> None:
        """Execute one build, then release the running lock."""
        try:
            self._execute(job)
        finally:
            self._running_lock.release()

    def _run_cascade(self, jobs: list[BuildJob]) -> None:
        """Execute a root build then its descendants, under one lock hold.

        The root is jobs[0]. If it fails, the descendants are marked
        SKIPPED. A descendant that fails does NOT stop its siblings —
        they're independent images.
        """
        try:
            root = jobs[0]
            self._execute(root)
            root_ok = root.state == JobState.SUCCEEDED
            for job in jobs[1:]:
                if not root_ok:
                    job.state = JobState.SKIPPED
                    job.finished_at = time.time()
                    job.recent_lines.append(
                        f"skipped: cascade root build "
                        f"'{root.profile}' did not succeed"
                    )
                    continue
                self._execute(job)
        finally:
            self._running_lock.release()

    def _execute(self, job: BuildJob) -> None:
        """Run one build's `make image` subprocess and record the result.

        Does NOT touch the running lock — the caller owns that (a single
        build releases after one call; a cascade releases after the whole
        sequence). Fires the on_success callback for a SUCCEEDED job.
        """
        job.state = JobState.RUNNING
        job.started_at = time.time()
        try:
            assert job.log_path is not None
            with open(job.log_path, "w", buffering=1) as log_file:
                env = None
                cmd = [
                    self._make_path,
                    "image",
                    f"PROFILE={job.profile}",
                    f"ARCH={job.architecture}",
                ]
                # Per-profile `suite` file (Debian release codename, e.g.
                # "trixie", "bookworm") overrides the recipe default if
                # present. Lets a single dashboard manage profiles
                # pinned to different Debian releases without forking
                # the recipe.
                suite_file = (
                    self._repo_root / "image" / "profiles"
                    / job.profile / "suite"
                )
                if suite_file.is_file():
                    suite = suite_file.read_text().strip().splitlines()
                    if suite and suite[0].strip():
                        cmd.append(f"SUITE={suite[0].strip()}")
                # Stream stdout+stderr line-by-line into both the file and
                # the recent_lines ring.
                process = subprocess.Popen(
                    cmd,
                    cwd=str(self._repo_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=env,
                    bufsize=1,
                )
                assert process.stdout is not None
                for raw_line in process.stdout:
                    line = raw_line.rstrip("\n")
                    log_file.write(line + "\n")
                    job.recent_lines.append(line)
                return_code = process.wait()
            job.exit_code = return_code
            outcome = (
                JobState.SUCCEEDED if return_code == 0 else JobState.FAILED
            )
        except Exception as exc:  # noqa: BLE001 — surface every error
            job.recent_lines.append(f"build job crashed: {exc!r}")
            outcome = JobState.FAILED
            job.exit_code = -1
        # The running lock is owned by the caller (_run_single releases
        # after this one build; _run_cascade after the whole sequence),
        # so we do NOT release it here. The success callback still fires
        # per build as it completes — it only arms reboots on the
        # registry, which the lock never guarded. Callback exceptions are
        # swallowed into the job log — a fleet-wide reboot failure
        # shouldn't retroactively "fail" a build that produced a valid
        # artifact.
        if outcome == JobState.SUCCEEDED and self._on_success is not None:
            try:
                self._on_success(job)
            except Exception as exc:  # noqa: BLE001
                job.recent_lines.append(
                    f"on_success callback failed: {exc!r}"
                )
        # Published last: pollers stop at the first terminal state, so
        # whatever the callback recorded must already be on the job.
        job.finished_at = time.time()
        job.state = outcome


def _make_id() -> str:
    """A short, URL-safe job id; collisions are negligible at our scale."""
    return secrets.token_hex(6)


# The implicit root every profile inherits from (mirrors the resolver's
# BASE_PROFILE). Making it every profile's parent here is what turns a
# build of `base` into a rebuild of the whole fleet.
BASE_PROFILE = "base"


def _read_profile_parents(profiles_root: Path, name: str) -> list[str]:
    """The profiles a given profile inherits from, including the implicit
    `base` root (prepended to every profile except `base` itself, when a
    `base` profile exists). Declared parents come from its `parent` file,
    one per line, `#` comments and blanks ignored."""
    parents: list[str] = []
    parent_file = profiles_root / name / "parent"
    if parent_file.is_file():
        for line in parent_file.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                parents.append(stripped)
    if (
        name != BASE_PROFILE
        and BASE_PROFILE not in parents
        and (profiles_root / BASE_PROFILE).is_dir()
    ):
        parents.insert(0, BASE_PROFILE)
    return parents


def compute_build_cascade(profiles_root: Path, root: str) -> list[str]:
    """Ordered build list for rebuilding `root` and everything downstream.

    Returns `[root, *descendants]` where descendants are every profile
    whose inheritance chain includes `root`, ordered so a profile always
    appears after the parents it inherits from (a topological order). A
    profile's built image embeds its whole resolved chain, so when a
    parent changes, its descendants are stale until rebuilt.

    Degrades to `[root]` when there is no profiles directory (e.g. a
    test repo) or `root` has no descendants.
    """
    if not profiles_root.is_dir():
        return [root]
    profiles = [d.name for d in profiles_root.iterdir() if d.is_dir()]
    # Direct-children adjacency (reverse of the parent edges).
    children: dict[str, list[str]] = {p: [] for p in profiles}
    for profile in profiles:
        for parent in _read_profile_parents(profiles_root, profile):
            children.setdefault(parent, []).append(profile)
    # Collect the transitive descendant set of `root`.
    in_set = {root}
    stack = list(children.get(root, []))
    while stack:
        node = stack.pop()
        if node in in_set:
            continue
        in_set.add(node)
        stack.extend(children.get(node, []))
    # Topological sort within the set: emit a node once every parent of
    # it that is also in the set has been emitted. Ties broken
    # alphabetically for a stable, readable order.
    remaining_parents = {
        node: {
            parent
            for parent in _read_profile_parents(profiles_root, node)
            if parent in in_set
        }
        for node in in_set
    }
    ordered: list[str] = []
    while remaining_parents:
        ready = sorted(
            node for node, parents in remaining_parents.items() if not parents
        )
        if not ready:
            # A cycle (the resolver forbids these) — fall back to a stable
            # order rather than looping forever.
            ordered.extend(sorted(remaining_parents))
            break
        for node in ready:
            ordered.append(node)
            del remaining_parents[node]
            for parents in remaining_parents.values():
                parents.discard(node)
    # `root` has no in-set parents, so it always sorts first.
    return ordered


def make_compat_shutil_which_check() -> bool:
    """Tiny convenience so callers can detect make is on PATH."""
    return shutil.which("make") is not None
