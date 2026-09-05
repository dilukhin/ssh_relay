"""Build-time внедрение exact source SHA в runtime ssh_relay."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

ROOT = Path(__file__).resolve().parent
BUILD_METADATA = ROOT / "ssh_relay_build.py"
_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
_SOURCE_ASSIGNMENT = re.compile(
    r'^_SOURCE_SHA: str \| None = (?:None|["\'][0-9a-fA-F]{40}["\'])$',
    re.MULTILINE,
)


def _normalize_sha(value: object, *, source: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not _SHA_PATTERN.fullmatch(text):
        raise RuntimeError(f"{source} должен содержать полный 40-hex Git SHA.")
    return text.lower()


def _embedded_sha() -> str | None:
    text = BUILD_METADATA.read_text(encoding="utf-8")
    match = re.search(r'^_SOURCE_SHA: str \| None = ["\']([0-9a-fA-F]{40})["\']$', text, re.MULTILINE)
    return _normalize_sha(match.group(1), source="Встроенный source SHA") if match else None


def _git_source_sha() -> str | None:
    if not (ROOT / ".git").exists():
        return None
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if status.returncode != 0:
        raise RuntimeError(f"Не удалось проверить состояние Git перед сборкой: {status.stderr.strip()}")
    if status.stdout.strip():
        raise RuntimeError(
            "Нельзя встроить exact source SHA: tracked-файлы исходного дерева имеют незакоммиченные изменения."
        )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Не удалось определить Git HEAD для сборки: {result.stderr.strip()}")
    return _normalize_sha(result.stdout, source="Git HEAD")


def resolve_source_sha() -> str:
    """Определяет provenance текущего build input без runtime-чтения .git."""
    explicit = _normalize_sha(os.environ.get("SSH_RELAY_SOURCE_SHA"), source="SSH_RELAY_SOURCE_SHA")
    git_sha = _git_source_sha()
    if git_sha is not None:
        if explicit is not None and explicit != git_sha:
            raise RuntimeError("SSH_RELAY_SOURCE_SHA не совпадает с exact Git HEAD собираемого дерева.")
        return git_sha
    if explicit is not None:
        return explicit
    embedded = _embedded_sha()
    if embedded is not None:
        return embedded
    raise RuntimeError(
        "Не удалось определить exact source SHA. Для сборки вне Git checkout задайте SSH_RELAY_SOURCE_SHA."
    )


def render_build_metadata(path: Path, exact_sha: str) -> None:
    text = path.read_text(encoding="utf-8")
    rendered, count = _SOURCE_ASSIGNMENT.subn(f'_SOURCE_SHA: str | None = "{exact_sha}"', text, count=1)
    if count != 1:
        raise RuntimeError(f"Не найдена единственная точка внедрения source SHA в {path}.")
    path.write_text(rendered, encoding="utf-8", newline="\n")


class BuildPyWithSourceSha(build_py):
    def run(self) -> None:
        exact_sha = resolve_source_sha()
        super().run()
        render_build_metadata(Path(self.build_lib) / "ssh_relay_build.py", exact_sha)


class SdistWithSourceSha(sdist):
    def make_release_tree(self, base_dir: str, files: list[str]) -> None:
        exact_sha = resolve_source_sha()
        super().make_release_tree(base_dir, files)
        render_build_metadata(Path(base_dir) / "ssh_relay_build.py", exact_sha)


setup(cmdclass={"build_py": BuildPyWithSourceSha, "sdist": SdistWithSourceSha})
