"""Smoke tests for the verify_/review_/coding_server_* event builders."""

from __future__ import annotations

from shared.events import (
    coding_server_boot_failed,
    review_skipped_no_runner,
    review_ui_check_started,
    verify_failed,
    verify_passed,
    verify_skipped_no_runner,
    verify_started,
)


def test_verify_started_payload():
    e = verify_started(task_id=42, cycle=1)
    assert e.type == "task.verify_started"
    assert e.payload == {"cycle": 1}
    assert e.task_id == 42


def test_verify_passed_payload():
    e = verify_passed(task_id=7, cycle=3)
    assert e.type == "task.verify_passed"
    assert e.payload == {"cycle": 3}
    assert e.task_id == 7


def test_verify_failed_payload():
    e = verify_failed(task_id=42, cycle=2, reason="boot_timeout")
    assert e.type == "task.verify_failed"
    assert e.payload["reason"] == "boot_timeout"
    assert e.payload["cycle"] == 2


def test_verify_skipped_no_runner():
    e = verify_skipped_no_runner(task_id=5)
    assert e.type == "task.verify_skipped_no_runner"
    assert e.payload == {}
    assert e.task_id == 5


def test_coding_server_boot_failed():
    e = coding_server_boot_failed(task_id=42, reason="no run command")
    assert e.type == "task.coding_server_boot_failed"
    assert e.payload == {"reason": "no run command"}


def test_review_ui_check_started():
    e = review_ui_check_started(task_id=10, cycle=1)
    assert e.type == "task.review_ui_check_started"
    assert e.payload == {"cycle": 1}


def test_review_skipped_no_runner():
    e = review_skipped_no_runner(task_id=8)
    assert e.type == "task.review_skipped_no_runner"
    assert e.payload == {}
