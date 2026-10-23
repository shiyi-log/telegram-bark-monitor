"""执行 Fuwu 工作流中内嵌的配置检查与预检脚本。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/fuwu-publish.yml"


def _workflow_run_script(step_name: str) -> str:
    """无需额外 YAML 依赖，提取指定步骤的字面 run 脚本。"""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    marker = f"      - name: {step_name}"
    try:
        step_start = lines.index(marker)
    except ValueError as error:
        raise AssertionError(f"workflow step not found: {step_name}") from error

    step_end = next(
        (
            index
            for index in range(step_start + 1, len(lines))
            if lines[index].startswith("      - name: ")
        ),
        len(lines),
    )
    run_line = next(
        (index for index in range(step_start + 1, step_end) if lines[index] == "        run: |"),
        None,
    )
    if run_line is None:
        raise AssertionError(f"literal run block not found for step: {step_name}")

    body: list[str] = []
    for line in lines[run_line + 1 :]:
        if line and not line.startswith("          "):
            break
        body.append(line[10:] if line else "")
    return "\n".join(body).rstrip() + "\n"


def _run_script(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=ROOT,
        env={**os.environ, **env},
        capture_output=True,
        check=False,
        text=True,
    )


def _run_configuration(
    tmp_path: Path, **overrides: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    output_path = tmp_path / "github-output"
    summary_path = tmp_path / "github-summary"
    env = {
        "FUWU_ENABLED": "",
        "FUWU_BASE_URL": "",
        "FUWU_PUBLIC_BASE_URL": "",
        "FUWU_TOKEN": "",
        "FUWU_USERNAME": "",
        "FUWU_PASSWORD": "",
        "MANUAL_RUN": "false",
        "GITHUB_OUTPUT": str(output_path),
        "GITHUB_STEP_SUMMARY": str(summary_path),
        **overrides,
    }
    try:
        result = _run_script(_workflow_run_script("Check configuration"), env)
        output = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        return result, output
    finally:
        output_path.unlink(missing_ok=True)
        summary_path.unlink(missing_ok=True)


def test_enabled_with_complete_configuration_sets_ready_true(tmp_path: Path) -> None:
    result, output = _run_configuration(
        tmp_path,
        FUWU_ENABLED="true",
        FUWU_BASE_URL="https://fuwu.example",
        FUWU_PUBLIC_BASE_URL="https://downloads.example",
        FUWU_TOKEN="test-token",
    )

    assert result.returncode == 0, result.stderr
    assert output == "ready=true\n"


@pytest.mark.parametrize("enabled", ["false", ""])
def test_disabled_setting_ignores_residual_credentials_and_urls(
    tmp_path: Path, enabled: str
) -> None:
    result, output = _run_configuration(
        tmp_path,
        FUWU_ENABLED=enabled,
        FUWU_BASE_URL="https://fuwu.example",
        FUWU_PUBLIC_BASE_URL="https://downloads.example",
        FUWU_TOKEN="leftover-token",
    )

    assert result.returncode == 0, result.stderr
    assert output == "ready=false\n"


def test_invalid_enabled_setting_fails_without_marking_workflow_ready(tmp_path: Path) -> None:
    result, output = _run_configuration(
        tmp_path,
        FUWU_ENABLED="yes",
        FUWU_BASE_URL="https://fuwu.example",
        FUWU_TOKEN="test-token",
    )

    assert result.returncode != 0
    assert "FUWU_ENABLED 必须为 true 或 false" in result.stderr
    assert "ready=true" not in output


def test_enabled_without_authentication_fails_closed(tmp_path: Path) -> None:
    result, output = _run_configuration(
        tmp_path,
        FUWU_ENABLED="true",
        FUWU_BASE_URL="https://fuwu.example",
        FUWU_PUBLIC_BASE_URL="https://downloads.example",
        FUWU_USERNAME="publisher",
    )

    assert result.returncode != 0
    assert "认证 secrets" in result.stderr
    assert "ready=true" not in output


def test_enabled_without_anonymous_download_address_fails_closed(tmp_path: Path) -> None:
    result, output = _run_configuration(
        tmp_path,
        FUWU_ENABLED="true",
        FUWU_BASE_URL="https://fuwu.example",
        FUWU_TOKEN="test-token",
    )

    assert result.returncode != 0
    assert "匿名下载基地址" in result.stderr
    assert "ready=true" not in output


def test_api_beta_is_rejected_before_git_fetch(tmp_path: Path) -> None:
    git_called = tmp_path / "git-called"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    git_stub = stub_dir / "git"
    git_stub.write_text(
        '#!/bin/sh\nprintf called > "$FUWU_TEST_GIT_CALLED"\nexit 99\n',
        encoding="utf-8",
    )
    git_stub.chmod(0o755)

    result = _run_script(
        _workflow_run_script("Download and bind the Release manifest"),
        {
            "PATH": os.pathsep.join((str(stub_dir), os.environ["PATH"])),
            "FUWU_TEST_GIT_CALLED": str(git_called),
            "GH_TOKEN": "unused-test-token",
            "GH_REPO": "example/bark",
            "RELEASE_TAG": "v0.1.1",
            "RELEASE_CHANNEL": "beta",
            "FUWU_PUBLIC_MODE": "api",
        },
    )

    assert result.returncode != 0
    assert "api 模式不能发布 beta" in result.stderr
    assert not git_called.exists()
