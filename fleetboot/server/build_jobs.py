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
from typing import Optional


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


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

    def start(
        self, *, profile: str, architecture: str = "amd64",
    ) -> BuildJob:
        """Spawn a build. Raises if one is already running."""
        if not self._running_lock.acquire(blocking=False):
            raise BuildAlreadyRunningError(
                "another build is already running"
            )
        job_id = _make_id()
        log_path = self._log_dir / f"{job_id}.log"
        job = BuildJob(
            job_id=job_id,
            profile=profile,
            architecture=architecture,
            state=JobState.RUNNING,
            started_at=time.time(),
            log_path=str(log_path),
        )
        with self._jobs_lock:
            self._jobs[job_id] = job
        thread = threading.Thread(
            target=self._run, args=(job,), daemon=True,
            name=f"build-{job_id}",
        )
        thread.start()
        return job

    # ---- internals ------------------------------------------------------

    def _run(self, job: BuildJob) -> None:
        """Execute the build subprocess; release running_lock at the end."""
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
            job.state = (
                JobState.SUCCEEDED if return_code == 0 else JobState.FAILED
            )
        except Exception as exc:  # noqa: BLE001 — surface every error
            job.recent_lines.append(f"build job crashed: {exc!r}")
            job.state = JobState.FAILED
            job.exit_code = -1
        finally:
            job.finished_at = time.time()
            self._running_lock.release()


def _make_id() -> str:
    """A short, URL-safe job id; collisions are negligible at our scale."""
    return secrets.token_hex(6)


def make_compat_shutil_which_check() -> bool:
    """Tiny convenience so callers can detect make is on PATH."""
    return shutil.which("make") is not None
