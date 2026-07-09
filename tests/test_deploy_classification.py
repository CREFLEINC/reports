"""ops/deploy.sh 가 지켜야 할 계약(contract)을 배포 전 테스트 게이트에서 강제한다.

1. 재빌드 판정 ⊇ Dockerfile 의 COPY 목록 ('COPY 트랩')
   이미지에 들어가는 소스가 늘었는데 deploy.sh 의 재빌드 패턴에 추가하지 않으면, 그 파일만
   바뀐 커밋은 재빌드 없이 마커(.deployed_sha)만 전진한다. 운영은 조용히 낡은 코드로 남고
   이후 실행에서도 자가 복구되지 않는다. Dockerfile 이 진실의 출처다. (doctypes.py 누락)

2. 테스트 게이트는 워크스페이스에 쓰지 않는다
   게이트 컨테이너는 root 로 돈다. 워크스페이스를 쓰기 마운트하면 .pytest_cache/ 와 테스트가
   만드는 uploads/ 가 root 소유로 남고, 러너(hulk)가 지우지 못해 다음 실행의 `git clean -ffdx`
   가 EACCES 로 죽는다 — 배포가 자기 자신을 벽돌로 만든다.

둘 다 이슈 #4 에서 실제로 발생했다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SH = ROOT / "ops" / "deploy.sh"


def copy_sources(dockerfile: Path) -> list[str]:
    """Dockerfile 의 COPY 지시문에서 소스 경로만 뽑는다(마지막 토큰은 목적지)."""
    sources: list[str] = []
    for raw in dockerfile.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not re.match(r"^COPY\s", line, re.IGNORECASE):
            continue
        tokens = [t for t in line.split()[1:] if not t.startswith("--")]
        sources.extend(tokens[:-1])  # 마지막 = 목적지
    return sources


def rebuild_pattern(var: str) -> str:
    """deploy.sh 에서 `matches '<ERE>'; then <var>=1` 형태의 판정 정규식을 추출한다."""
    script = DEPLOY_SH.read_text(encoding="utf-8")
    match = re.search(rf"matches\s+'([^']+)'\s*;\s*then\s+{var}=1", script)
    assert match, f"deploy.sh 에서 {var} 판정 패턴을 찾지 못했다"
    return match.group(1)


VIEWER_CASES = [(src, "BUILD_VIEWER") for src in copy_sources(ROOT / "Dockerfile")]
VIEWER_CASES.append(("Dockerfile", "BUILD_VIEWER"))
RENDERER_CASES = [(src, "BUILD_RENDERER") for src in copy_sources(ROOT / "Dockerfile.renderer")]
RENDERER_CASES.append(("Dockerfile.renderer", "BUILD_RENDERER"))


@pytest.mark.parametrize("source,var", VIEWER_CASES + RENDERER_CASES)
def test_copied_source_triggers_rebuild(source: str, var: str) -> None:
    """이미지에 COPY 되는 모든 소스는 해당 이미지의 재빌드를 유발해야 한다."""
    pattern = rebuild_pattern(var)
    assert re.match(pattern, source), (
        f"{source!r} 가 이미지에 COPY 되는데 deploy.sh 의 {var} 패턴에 없다.\n"
        f"  패턴: {pattern}\n"
        f"  → 이 파일만 바뀐 커밋은 재빌드 없이 마커만 전진해 운영이 낡은 코드로 남는다."
    )


def test_viewer_and_renderer_patterns_are_disjoint() -> None:
    """뷰어 패턴이 Dockerfile.renderer 를 삼키지 않아야 한다(`Dockerfile` 앵커 누락 방지)."""
    assert not re.match(rebuild_pattern("BUILD_VIEWER"), "Dockerfile.renderer")


def pytest_gate_command() -> str:
    """deploy.sh 의 테스트 게이트 `docker run …` 명령(줄바꿈 이어붙임)."""
    script = DEPLOY_SH.read_text(encoding="utf-8").replace("\\\n", " ")
    match = re.search(r"docker run .*?python:3\.12-slim", script)
    assert match, "deploy.sh 에서 pytest 게이트의 docker run 명령을 찾지 못했다"
    return match.group(0)


def test_pytest_gate_mounts_workspace_read_only() -> None:
    """게이트가 워크스페이스를 바인드하는 모든 마운트는 :ro 여야 한다."""
    command = pytest_gate_command()
    mounts = re.findall(r'-v\s+"\$GITHUB_WORKSPACE":(\S+)', command)
    assert mounts, f"게이트가 워크스페이스를 마운트하지 않는다: {command}"
    for mount in mounts:
        assert mount.endswith(":ro"), (
            f"테스트 게이트가 워크스페이스를 쓰기 가능하게 마운트한다: -v $GITHUB_WORKSPACE:{mount}\n"
            f"  → 컨테이너 root 가 .pytest_cache/·uploads/ 를 root 소유로 남기면\n"
            f"    러너가 지우지 못해 다음 실행의 `git clean -ffdx` 가 EACCES 로 죽는다."
        )
