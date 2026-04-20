"""Tests for agent/tools/test_runner.py — framework detection, command building, output parsing."""

import os

import pytest

from agent.tools.test_runner import _build_command, _detect_framework, _parse_output


class TestDetectFramework:
    def test_detects_pytest_from_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        assert _detect_framework(str(tmp_path)) == "pytest"

    def test_detects_pytest_from_setup_py(self, tmp_path):
        (tmp_path / "setup.py").write_text("from setuptools import setup\n")
        assert _detect_framework(str(tmp_path)) == "pytest"

    def test_detects_pytest_from_ini(self, tmp_path):
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        assert _detect_framework(str(tmp_path)) == "pytest"

    def test_detects_jest_from_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"devDependencies": {"jest": "^29.0"}}'
        )
        assert _detect_framework(str(tmp_path)) == "jest"

    def test_detects_vitest_from_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"devDependencies": {"vitest": "^1.0"}}'
        )
        assert _detect_framework(str(tmp_path)) == "vitest"

    def test_detects_mocha_from_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"devDependencies": {"mocha": "^10.0"}}'
        )
        assert _detect_framework(str(tmp_path)) == "mocha"

    def test_detects_npm_test_as_fallback(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"scripts": {"test": "echo ok"}}'
        )
        assert _detect_framework(str(tmp_path)) == "npm"

    def test_detects_go(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/x\n")
        assert _detect_framework(str(tmp_path)) == "go"

    def test_detects_cargo(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname = 'x'\n")
        assert _detect_framework(str(tmp_path)) == "cargo"

    def test_detects_rspec(self, tmp_path):
        (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\n")
        assert _detect_framework(str(tmp_path)) == "rspec"

    def test_detects_pytest_from_test_file(self, tmp_path):
        (tmp_path / "test_app.py").write_text("def test_x(): pass\n")
        assert _detect_framework(str(tmp_path)) == "pytest"

    def test_returns_none_for_empty_dir(self, tmp_path):
        assert _detect_framework(str(tmp_path)) is None


class TestBuildCommand:
    def test_pytest_default(self):
        cmd = _build_command("pytest", "")
        assert "pytest" in cmd
        assert "-v" in cmd

    def test_pytest_with_target(self):
        cmd = _build_command("pytest", "tests/test_app.py")
        assert "tests/test_app.py" in cmd

    def test_jest_default(self):
        cmd = _build_command("jest", "")
        assert "jest" in cmd

    def test_go_default(self):
        cmd = _build_command("go", "")
        assert "go test ./..." in cmd

    def test_go_with_target(self):
        cmd = _build_command("go", "./pkg/...")
        assert "./pkg/..." in cmd

    def test_unknown_framework(self):
        assert _build_command("unknown_framework", "") is None


class TestParseOutput:
    def test_pytest_passed(self):
        output = "5 passed in 1.23s"
        summary = _parse_output("pytest", output, 0)
        assert "PASSED" in summary
        assert "Passed: 5" in summary
        assert "Failed: 0" in summary

    def test_pytest_failed(self):
        output = "3 passed, 2 failed, 1 error in 2.00s"
        summary = _parse_output("pytest", output, 1)
        assert "FAILED" in summary
        assert "Passed: 3" in summary
        assert "Failed: 2" in summary
        assert "Errors: 1" in summary

    def test_pytest_with_skipped(self):
        output = "10 passed, 2 skipped in 0.50s"
        summary = _parse_output("pytest", output, 0)
        assert "Skipped: 2" in summary

    def test_jest_output(self):
        output = "Tests: 1 failed, 4 passed, 5 total"
        summary = _parse_output("jest", output, 1)
        assert "FAILED" in summary
        assert "Passed: 4" in summary
        assert "Failed: 1" in summary

    def test_go_output(self):
        output = "--- PASS: TestFoo\n--- PASS: TestBar\n--- FAIL: TestBaz\n"
        summary = _parse_output("go", output, 1)
        assert "FAILED" in summary
        assert "Passed: 2" in summary
        assert "Failed: 1" in summary

    def test_no_counts_available(self):
        summary = _parse_output("cargo", "some output", 0)
        assert "PASSED" in summary
        assert "exit code 0" in summary
