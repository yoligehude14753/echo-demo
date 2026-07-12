from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from app.adapters.skill.python_executor import (
    PACKAGED_PYTHON_WORKER_FLAG,
    _python_process_argv,
    exec_python_to_artifact,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = BACKEND_ROOT / "packaging" / "entrypoint.py"
WORKER_PROBE = BACKEND_ROOT / "tests" / "fixtures" / "packaged_worker_probe.py"
ARTIFACT_RUNTIME_SMOKE_FLAG = "--artifact-runtime-smoke"


def _run_worker(
    script_path: Path | str, *script_args: str, cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ENTRYPOINT),
            PACKAGED_PYTHON_WORKER_FLAG,
            str(script_path),
            *script_args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


@pytest.mark.unit
def test_worker_cli_preserves_script_argv_and_cwd(tmp_path: Path) -> None:
    result_path = tmp_path / "worker-result.json"

    completed = _run_worker(
        WORKER_PROBE,
        str(result_path),
        "alpha",
        "--beta",
        cwd=WORKER_PROBE.parent,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload == {
        "argv": [str(WORKER_PROBE), str(result_path), "alpha", "--beta"],
        "cwd": str(WORKER_PROBE.parent),
        "file": str(WORKER_PROBE),
    }


@pytest.mark.unit
def test_worker_cli_rejects_relative_or_out_of_cwd_script(tmp_path: Path) -> None:
    script_path = tmp_path / "fixture.py"
    script_path.write_text("raise SystemExit(0)\n", encoding="utf-8")

    relative = _run_worker(script_path.name, cwd=tmp_path)
    outside = _run_worker(script_path.resolve(), cwd=tmp_path.parent)

    assert relative.returncode == 2
    assert "must be absolute" in relative.stderr
    assert outside.returncode == 2
    assert "worker cwd" in outside.stderr


@pytest.mark.unit
def test_worker_cli_propagates_exit_code_and_traceback() -> None:
    exited = _run_worker(
        WORKER_PROBE,
        "--exit-seven",
        cwd=WORKER_PROBE.parent,
    )
    failed = _run_worker(WORKER_PROBE, "--fail", cwd=WORKER_PROBE.parent)

    assert exited.returncode == 7
    assert failed.returncode == 1
    assert "Traceback (most recent call last)" in failed.stderr
    assert "RuntimeError: packaged-worker-probe-failure" in failed.stderr
    assert str(WORKER_PROBE) in failed.stderr


@pytest.mark.unit
def test_python_process_argv_switches_only_when_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_path = tmp_path / "script.py"
    script_path.write_text("pass\n", encoding="utf-8")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", "/runtime/python")
    assert _python_process_argv(script_path) == [
        "/runtime/python",
        str(script_path.resolve()),
    ]

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/runtime/echodesk-backend")
    assert _python_process_argv(script_path) == [
        "/runtime/echodesk-backend",
        PACKAGED_PYTHON_WORKER_FLAG,
        str(script_path.resolve()),
    ]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_frozen_executor_uses_hidden_worker_and_preserves_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.update({"argv": argv, **kwargs})
        (Path(kwargs["cwd"]) / "output.docx").write_bytes(b"artifact" * 32)
        return subprocess.CompletedProcess(argv, 0, stdout="ignored", stderr="")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/runtime/echodesk-backend")
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = await exec_python_to_artifact(
        "from docx import Document\ndoc = Document()\ndoc.save('x.docx')\n",
        tmp_path,
        expected_ext="docx",
        timeout_s=13.0,
        env={"ECHODESK_WORKER_TEST": "1"},
    )

    assert result.success is True
    assert result.output_path == tmp_path / "output.docx"
    assert captured["argv"] == [
        "/runtime/echodesk-backend",
        PACKAGED_PYTHON_WORKER_FLAG,
        str((tmp_path / "script.py").resolve()),
    ]
    assert captured["cwd"] == str(tmp_path)
    assert captured["timeout"] == 13.0
    assert captured["capture_output"] is True
    assert captured["text"] is True
    assert captured["check"] is False
    assert captured["env"]["ECHODESK_WORKER_TEST"] == "1"


@pytest.mark.unit
def test_artifact_runtime_smoke_generates_openable_office_pdf_and_pptx(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "packaged-smoke"
    completed = subprocess.run(
        [
            sys.executable,
            str(ENTRYPOINT),
            ARTIFACT_RUNTIME_SMOKE_FLAG,
            str(output_dir),
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads((output_dir / "artifact-runtime-smoke.json").read_text("utf-8"))
    assert manifest["ok"] is True
    assert set(manifest["artifacts"]) == {"docx", "pdf", "pptx", "xlsx"}
    assert manifest["artifacts"]["docx"]["size_bytes"] > 1_000
    assert manifest["artifacts"]["xlsx"]["size_bytes"] > 1_000
    assert manifest["artifacts"]["pdf"]["size_bytes"] > 100
    assert manifest["artifacts"]["pptx"]["size_bytes"] > 8_000
    diarizer = manifest["diarizer_runtime"]
    assert diarizer["cpu_only"] is True
    assert diarizer["cuda_available"] is False
    assert diarizer["cuda_build"] is None
    assert diarizer["distributed_available"] is True
    assert diarizer["jit_enabled"] is False
    assert diarizer["torch_version"].startswith("2.11.0")
    assert diarizer["torchaudio_version"].startswith("2.11.0")
    assert diarizer["vector_norm"] == pytest.approx(1.0)
