"""Simple task queue — processes jobs from a database table.

Currently uses a naive polling loop that queries the database every second.
This works but has scaling issues (DB load, latency, wasted queries).
The architecture needs improvement.
"""

import sqlite3
import time
from dataclasses import dataclass
from enum import Enum


class JobStatus(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Job:
    id: int
    task_type: str
    payload: str
    status: JobStatus
    result: str | None = None
    error: str | None = None


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect("jobs.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            result TEXT,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


def submit_job(task_type: str, payload: str) -> int:
    """Submit a new job to the queue."""
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO jobs (task_type, payload, status) VALUES (?, ?, ?)",
        (task_type, payload, JobStatus.PENDING.value),
    )
    conn.commit()
    job_id = cursor.lastrowid
    conn.close()
    return job_id


def get_job_status(job_id: int) -> Job | None:
    """Check the status of a job."""
    conn = get_db()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return Job(
        id=row[0], task_type=row[1], payload=row[2],
        status=JobStatus(row[3]), result=row[4], error=row[5],
    )


# --- Worker: polls for pending jobs ---

def process_job(job: Job) -> str:
    """Process a single job. Returns the result."""
    if job.task_type == "echo":
        return f"echo: {job.payload}"
    elif job.task_type == "reverse":
        return job.payload[::-1]
    elif job.task_type == "upper":
        return job.payload.upper()
    else:
        raise ValueError(f"Unknown task type: {job.task_type}")


def worker_loop(max_iterations: int = None):
    """Poll for pending jobs and process them.

    This is the naive implementation that needs architectural improvement.
    Problems:
    - Polls every second even when no jobs exist (wastes DB connections)
    - No concurrent job processing (one at a time)
    - No retry logic for failed jobs
    - No way to notify submitters when jobs complete
    - No priority support
    """
    conn = get_db()
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY id LIMIT 1",
            (JobStatus.PENDING.value,),
        ).fetchone()

        if row:
            job = Job(
                id=row[0], task_type=row[1], payload=row[2],
                status=JobStatus(row[3]), result=row[4], error=row[5],
            )
            conn.execute(
                "UPDATE jobs SET status = ? WHERE id = ?",
                (JobStatus.PROCESSING.value, job.id),
            )
            conn.commit()

            try:
                result = process_job(job)
                conn.execute(
                    "UPDATE jobs SET status = ?, result = ? WHERE id = ?",
                    (JobStatus.COMPLETED.value, result, job.id),
                )
            except Exception as e:
                conn.execute(
                    "UPDATE jobs SET status = ?, error = ? WHERE id = ?",
                    (JobStatus.FAILED.value, str(e), job.id),
                )
            conn.commit()
        else:
            time.sleep(1)  # Wasteful polling

        iterations += 1
    conn.close()
