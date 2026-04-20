"""Tests for the job queue — must continue to pass after refactoring."""

import os
import sqlite3
import threading
import time

from app import (
    Job,
    JobStatus,
    get_db,
    get_job_status,
    process_job,
    submit_job,
    worker_loop,
)


def setup_module():
    """Clean database before tests."""
    if os.path.exists("jobs.db"):
        os.remove("jobs.db")


def test_submit_job():
    job_id = submit_job("echo", "hello")
    assert job_id is not None
    assert job_id > 0


def test_get_job_status():
    job_id = submit_job("echo", "test")
    job = get_job_status(job_id)
    assert job is not None
    assert job.status == JobStatus.PENDING
    assert job.payload == "test"


def test_process_echo():
    job = Job(id=1, task_type="echo", payload="hi", status=JobStatus.PENDING)
    assert process_job(job) == "echo: hi"


def test_process_reverse():
    job = Job(id=2, task_type="reverse", payload="abc", status=JobStatus.PENDING)
    assert process_job(job) == "cba"


def test_process_upper():
    job = Job(id=3, task_type="upper", payload="hello", status=JobStatus.PENDING)
    assert process_job(job) == "HELLO"


def test_worker_processes_job():
    """Submit a job and verify the worker picks it up."""
    if os.path.exists("jobs.db"):
        os.remove("jobs.db")
    job_id = submit_job("echo", "worker test")
    worker_loop(max_iterations=3)
    job = get_job_status(job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.result == "echo: worker test"


def test_worker_handles_unknown_type():
    """Worker should mark unknown job types as failed."""
    if os.path.exists("jobs.db"):
        os.remove("jobs.db")
    job_id = submit_job("nonexistent", "data")
    worker_loop(max_iterations=3)
    job = get_job_status(job_id)
    assert job.status == JobStatus.FAILED
    assert "Unknown task type" in job.error
