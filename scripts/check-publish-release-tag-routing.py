#!/usr/bin/env python3
"""执行发布 workflow 内的 tag routing 区块，防止 stable/preview 路由漂移。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-release-assets.yml"
START = "          # BEGIN RELEASE TAG ROUTING"
END = "          # END RELEASE TAG ROUTING"
ACTIVE_SHA = "f6c85741123a07df4e491d79c99f1cf5a3c64bc8"
RETIRED_SHAS = (
    "28caaade04cdb2038c2950017cbf702f126252c1",
    "8164bbee96d8ab5771d66e251a83c204af49471c",
)


def routing_script() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count(START) == 1, "release tag routing start marker must be unique"
    assert text.count(END) == 1, "release tag routing end marker must be unique"
    try:
        body = text.split(START, 1)[1].split(END, 1)[0]
    except IndexError as exc:
        raise AssertionError("release tag routing markers are missing or duplicated") from exc
    lines = [line[10:] if line.startswith("          ") else line for line in body.splitlines()]
    return "\n".join(
        [
            "set -euo pipefail",
            *lines,
            "printf '%s\\t%s\\n' \"$package_version\" \"$release_channel\"",
        ]
    )


def route(tag: str, sha: str = ACTIVE_SHA) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "RELEASE_TAG": tag,
        "RELEASE_SHA": sha,
    }
    return subprocess.run(
        ["bash", "-c", routing_script()],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def assert_allowed(tag: str, expected: str) -> None:
    result = route(tag)
    assert result.returncode == 0, (tag, result.stderr)
    assert result.stdout.strip() == expected, (tag, result.stdout)


def assert_rejected(tag: str, sha: str = ACTIVE_SHA) -> None:
    result = route(tag, sha)
    assert result.returncode != 0, (tag, sha, result.stdout)


def main() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'test "$release_is_prerelease" = "true"' in workflow
    assert 'test "$release_is_prerelease" = "false"' in workflow

    assert_allowed("v0.3.3", "0.3.3\tstable")
    assert_allowed("v12.34.56", "12.34.56\tstable")
    assert_allowed("v0.3.3-preview.2", "0.3.3\tpreview")
    assert_allowed("v12.34.56-preview.19", "12.34.56\tpreview")

    for rejected in (
        "v0.3.3-preview.1",
        "0.3.3",
        "v0.3",
        "v0.3.3-preview",
        "v0.3.3-rc.1",
        "v0.3.3-preview.2-extra",
    ):
        assert_rejected(rejected)
    for retired_sha in RETIRED_SHAS:
        assert_rejected("v0.3.3-preview.2", retired_sha)

    print("release tag routing: 4 allowed, 8 rejected")


if __name__ == "__main__":
    main()
