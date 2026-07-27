"""Git-based change detection.

Two modes map to the spec's local vs. staging distinction:

* ``working``  - changes in the working tree that have NOT been staged yet
                 (``codetest run`` / ``codetest generate``).
* ``staged``   - changes that have been promoted to the staging area
                 (``codetest run --stage``).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List


class GitError(RuntimeError):
    pass


@dataclass
class FileDiff:
    path: str            # repo-relative
    diff_text: str
    added_lines: List[str]
    removed_lines: List[str]
    changed_new_lines: List[int]   # line numbers in the NEW version of the file
    is_new_file: bool


def _run_git(args: List[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def is_git_repo(cwd: Path) -> bool:
    try:
        out = _run_git(["rev-parse", "--is-inside-work-tree"], cwd)
        return out.strip() == "true"
    except GitError:
        return False


def _is_test_source(path: str) -> bool:
    """Exclude the test tree so the agent never analyzes its own generated tests."""
    norm = path.replace("\\", "/")
    return "/src/test/" in norm or norm.startswith("src/test/")


def _changed_java_paths(cwd: Path, mode: str) -> List[str]:
    if mode == "staged":
        out = _run_git(["diff", "--cached", "--name-only", "--", "*.java"], cwd)
        raw = [p for p in out.splitlines() if p.strip()]
    else:
        # working mode: tracked-but-unstaged changes + brand new untracked files
        tracked = _run_git(["diff", "--name-only", "--", "*.java"], cwd).splitlines()
        untracked = _run_git(
            ["ls-files", "--others", "--exclude-standard", "--", "*.java"], cwd
        ).splitlines()
        raw = [*tracked, *untracked]

    seen: Dict[str, None] = {}
    for p in raw:
        p = p.strip()
        if p and not _is_test_source(p):
            seen[p] = None
    return list(seen.keys())


def _parse_hunks(diff_text: str) -> tuple[List[str], List[str], List[int]]:
    """Extract added/removed lines and NEW-file line numbers touched."""
    added: List[str] = []
    removed: List[str] = []
    changed_new_lines: List[int] = []
    new_ln = 0
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            # @@ -a,b +c,d @@
            try:
                plus = line.split("+", 1)[1]
                new_ln = int(plus.split(",")[0].split(" ")[0])
            except (IndexError, ValueError):
                new_ln = 0
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
            changed_new_lines.append(new_ln)
            new_ln += 1
        elif line.startswith("-"):
            removed.append(line[1:])
        else:
            new_ln += 1
    return added, removed, changed_new_lines


def get_file_diffs(cwd: Path, mode: str = "working") -> List[FileDiff]:
    """Return per-file diffs for all changed Java files in ``mode``."""
    if not is_git_repo(cwd):
        raise GitError(f"{cwd} is not a git repository")

    diffs: List[FileDiff] = []
    for path in _changed_java_paths(cwd, mode):
        is_new = False
        if mode == "staged":
            diff_text = _run_git(["diff", "--cached", "--", path], cwd)
        else:
            # Untracked files have no diff; synthesize one treating all lines as added.
            tracked_diff = _run_git(["diff", "--", path], cwd)
            if tracked_diff.strip():
                diff_text = tracked_diff
            else:
                is_new = True
                content = (cwd / path).read_text(encoding="utf-8", errors="replace")
                body = "".join(f"+{ln}\n" for ln in content.splitlines())
                diff_text = f"@@ -0,0 +1,{len(content.splitlines())} @@\n{body}"

        added, removed, changed = _parse_hunks(diff_text)
        diffs.append(
            FileDiff(
                path=path,
                diff_text=diff_text,
                added_lines=added,
                removed_lines=removed,
                changed_new_lines=changed,
                is_new_file=is_new,
            )
        )
    return diffs
