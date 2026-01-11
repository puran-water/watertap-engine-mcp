"""Background Job Manager for WaterTAP Solver Operations.

Handles long-running solve operations in background processes with:
- Job submission and tracking
- Status polling
- Result retrieval
- Crash recovery
"""

import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
import threading


class JobStatus(Enum):
    """Status of a background job."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    """Background job specification."""

    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    job_type: str = ""  # "solve", "initialize", "diagnose"

    status: JobStatus = JobStatus.PENDING
    progress: float = 0.0
    message: str = ""

    # Timing
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    # Results
    result: Optional[Dict] = None
    error: Optional[str] = None

    # Process tracking
    pid: Optional[int] = None

    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: Dict) -> "Job":
        """Create from dictionary."""
        data = data.copy()
        data["status"] = JobStatus(data["status"])
        return cls(**data)


class JobManager:
    """Manager for background jobs.

    Jobs are executed in subprocess to avoid blocking the MCP server.
    Job state is persisted to allow recovery after crashes.
    """

    def __init__(self, jobs_dir: Path, worker_script: Optional[Path] = None):
        """Initialize job manager.

        Args:
            jobs_dir: Directory for job state persistence
            worker_script: Path to worker script (optional)
        """
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

        self.worker_script = worker_script or (
            Path(__file__).parent.parent / "worker.py"
        )

        self._jobs: Dict[str, Job] = {}
        self._processes: Dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

        # Load existing jobs from disk
        self._load_jobs()

    def _job_path(self, job_id: str) -> Path:
        """Get path for job state file."""
        return self.jobs_dir / f"{job_id}.json"

    def _load_jobs(self) -> None:
        """Load existing jobs from disk."""
        for path in self.jobs_dir.glob("*.json"):
            try:
                with open(path) as f:
                    data = json.load(f)
                job = Job.from_dict(data)
                self._jobs[job.job_id] = job

                # Mark orphaned running jobs as failed
                if job.status == JobStatus.RUNNING:
                    job.status = JobStatus.FAILED
                    job.error = "Job interrupted (server restart)"
                    self._save_job(job)
            except (json.JSONDecodeError, KeyError, TypeError):
                continue

    def _save_job(self, job: Job) -> None:
        """Save job state to disk."""
        path = self._job_path(job.job_id)
        with open(path, "w") as f:
            json.dump(job.to_dict(), f, indent=2)

    def submit(
        self,
        session_id: str,
        job_type: str,
        params: Optional[Dict] = None,
    ) -> Job:
        """Submit a new background job.

        Args:
            session_id: Associated session ID
            job_type: Type of job ("solve", "initialize", "diagnose")
            params: Job-specific parameters

        Returns:
            Created Job instance
        """
        with self._lock:
            job = Job(
                session_id=session_id,
                job_type=job_type,
            )
            self._jobs[job.job_id] = job
            self._save_job(job)

            # Start worker process
            self._start_worker(job, params or {})

            return job

    def _start_worker(self, job: Job, params: Dict) -> None:
        """Start a worker process for the job.

        Args:
            job: Job to execute
            params: Job parameters
        """
        # Write params to file for worker
        params_file = self.jobs_dir / f"{job.job_id}_params.json"
        with open(params_file, "w") as f:
            json.dump({
                "job_id": job.job_id,
                "session_id": job.session_id,
                "job_type": job.job_type,
                "params": params,
            }, f)

        # Start subprocess
        try:
            process = subprocess.Popen(
                [sys.executable, str(self.worker_script), str(params_file)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.jobs_dir.parent),
            )
            self._processes[job.job_id] = process
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now(timezone.utc).isoformat()
            job.pid = process.pid
            self._save_job(job)
        except Exception as e:
            job.status = JobStatus.FAILED
            job.error = str(e)
            self._save_job(job)

    def get_status(self, job_id: str) -> Optional[Job]:
        """Get current status of a job.

        Args:
            job_id: Job ID to query

        Returns:
            Job instance or None if not found
        """
        with self._lock:
            # Reload from disk to get latest state
            if job_id in self._jobs:
                path = self._job_path(job_id)
                if path.exists():
                    try:
                        with open(path) as f:
                            data = json.load(f)
                        self._jobs[job_id] = Job.from_dict(data)
                    except (json.JSONDecodeError, KeyError):
                        pass

            # Check if process is still running
            job = self._jobs.get(job_id)
            if job and job_id in self._processes:
                process = self._processes[job_id]
                if process.poll() is not None:
                    # Process finished - reload final state
                    path = self._job_path(job_id)
                    if path.exists():
                        with open(path) as f:
                            data = json.load(f)
                        job = Job.from_dict(data)
                        self._jobs[job_id] = job
                    del self._processes[job_id]

            return job

    def get_result(self, job_id: str) -> Optional[Dict]:
        """Get result of a completed job.

        Args:
            job_id: Job ID to query

        Returns:
            Result dict or None if not available
        """
        job = self.get_status(job_id)
        if job and job.status == JobStatus.COMPLETED:
            return job.result
        return None

    def cancel(self, job_id: str) -> bool:
        """Cancel a running job.

        Args:
            job_id: Job to cancel

        Returns:
            True if cancelled, False if not found or not cancellable
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False

            if job.status not in (JobStatus.PENDING, JobStatus.RUNNING):
                return False

            # Kill process if running
            if job_id in self._processes:
                process = self._processes[job_id]
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                del self._processes[job_id]

            job.status = JobStatus.CANCELLED
            job.completed_at = datetime.now(timezone.utc).isoformat()
            self._save_job(job)
            return True

    def list_jobs(
        self,
        session_id: Optional[str] = None,
        status: Optional[JobStatus] = None,
    ) -> List[Job]:
        """List jobs with optional filtering.

        Args:
            session_id: Filter by session
            status: Filter by status

        Returns:
            List of matching jobs
        """
        with self._lock:
            jobs = list(self._jobs.values())

            if session_id is not None:
                jobs = [j for j in jobs if j.session_id == session_id]

            if status is not None:
                jobs = [j for j in jobs if j.status == status]

            return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def cleanup_old_jobs(self, max_age_hours: int = 24) -> int:
        """Remove jobs older than max_age_hours.

        Args:
            max_age_hours: Maximum age in hours

        Returns:
            Number of jobs removed
        """
        with self._lock:
            removed = 0
            cutoff = datetime.now(timezone.utc).timestamp() - (max_age_hours * 3600)

            for job_id, job in list(self._jobs.items()):
                if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                    try:
                        created = datetime.fromisoformat(job.created_at.replace("Z", "+00:00"))
                        if created.timestamp() < cutoff:
                            # Remove job file
                            path = self._job_path(job_id)
                            if path.exists():
                                path.unlink()

                            # Remove params file
                            params_file = self.jobs_dir / f"{job_id}_params.json"
                            if params_file.exists():
                                params_file.unlink()

                            del self._jobs[job_id]
                            removed += 1
                    except (ValueError, TypeError):
                        continue

            return removed

    def update_job(
        self,
        job_id: str,
        status: Optional[JobStatus] = None,
        progress: Optional[float] = None,
        message: Optional[str] = None,
        result: Optional[Dict] = None,
        error: Optional[str] = None,
    ) -> None:
        """Update job state (called by worker process).

        Args:
            job_id: Job to update
            status: New status
            progress: Progress percentage (0-100)
            message: Status message
            result: Final result (for completed jobs)
            error: Error message (for failed jobs)
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return

            if status is not None:
                job.status = status
            if progress is not None:
                job.progress = progress
            if message is not None:
                job.message = message
            if result is not None:
                job.result = result
            if error is not None:
                job.error = error

            if status in (JobStatus.COMPLETED, JobStatus.FAILED):
                job.completed_at = datetime.now(timezone.utc).isoformat()

            self._save_job(job)


# Worker-side helper functions
def update_job_from_worker(jobs_dir: Path, job_id: str, **kwargs) -> None:
    """Update job state from worker process.

    This writes directly to the job file since the worker is a separate process.

    Args:
        jobs_dir: Jobs directory
        job_id: Job to update
        **kwargs: Fields to update
    """
    job_path = jobs_dir / f"{job_id}.json"
    if not job_path.exists():
        return

    with open(job_path) as f:
        data = json.load(f)

    for key, value in kwargs.items():
        if key == "status":
            data[key] = value.value if isinstance(value, JobStatus) else value
        else:
            data[key] = value

    if data.get("status") in ("completed", "failed"):
        data["completed_at"] = datetime.now(timezone.utc).isoformat()

    with open(job_path, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()  # Ensure data is written to disk
