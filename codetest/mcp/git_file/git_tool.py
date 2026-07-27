"""Git Tool — Staged/Unstaged Diff 조회 (노이즈 필터링).

Formatting-only edits are noise for a test generator, so the diff is taken
with whitespace and blank-line changes ignored by default
(:class:`~codetest.models.DiffOptions`). A file whose entire diff is
whitespace/newline churn is dropped from the scan and reported separately
instead of producing a pointless test.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, List, Set, Tuple

from ...models import DEFAULT_DIFF_OPTIONS, DiffOptions, DiffScan, FileDiff


class GitError(RuntimeError):
    pass


def _run_git(args: List[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def is_git_repo(cwd: Path) -> bool:
    try:
        return _run_git(["rev-parse", "--is-inside-work-tree"], cwd).strip() == "true"
    except GitError:
        return False


def _is_test_source(path: str) -> bool:
    """Exclude the test tree so the agent never analyzes its own generated tests."""
    norm = path.replace("\\", "/")
    return "/src/test/" in norm or norm.startswith("src/test/")


def _name_only(cwd: Path, mode: str, flags: List[str]) -> List[str]:
    args = ["diff"]
    if mode == "staged":
        args.append("--cached")
    args += [*flags, "--name-only", "--", "*.java"]
    return [p.strip() for p in _run_git(args, cwd).splitlines() if p.strip()]


def _changed_java_paths(
    cwd: Path, mode: str, options: DiffOptions
) -> Tuple[List[str], Set[str], List[str]]:
    """Return (changed paths, untracked paths, whitespace-only paths).

    The whitespace flags are applied to ``--name-only`` too, so a file that was
    merely reindented never even enters the pipeline. Comparing that list
    against the unfiltered one is what tells us *which* files were dropped, so
    the report can say so instead of silently ignoring them.
    """
    flags = options.git_flags()
    untracked: Set[str] = set()

    tracked = _name_only(cwd, mode, flags)
    filtered_out = (
        [p for p in _name_only(cwd, mode, []) if p not in set(tracked)] if flags else []
    )

    if mode == "staged":
        raw = list(tracked)
    else:
        # working mode: tracked-but-unstaged changes + brand new untracked files
        others = [p.strip() for p in _run_git(
            ["ls-files", "--others", "--exclude-standard", "--", "*.java"], cwd
        ).splitlines() if p.strip()]
        untracked = {p for p in others if not _is_test_source(p)}
        raw = [*tracked, *others]

    seen: Dict[str, None] = {}
    for p in raw:
        if p and not _is_test_source(p):
            seen[p] = None
    return (list(seen.keys()), untracked,
            [p for p in filtered_out if not _is_test_source(p)])


def _is_noise(line: str, options: DiffOptions) -> bool:
    """True for a +/- line that carries no semantic change."""
    return options.ignore_blank_lines and not line.strip()


def parse_hunks(
    diff_text: str, options: DiffOptions = DEFAULT_DIFF_OPTIONS
) -> Tuple[List[str], List[str], List[int]]:
    """Extract added/removed lines and NEW-file line numbers touched.

    Blank/whitespace-only +/- lines are skipped when the options say so, but
    the new-file line counter still advances so line numbers stay accurate.
    """
    added: List[str] = []
    removed: List[str] = []
    changed_new_lines: List[int] = []
    new_ln = 0
    in_hunk = False

    for line in diff_text.splitlines():
        if line.startswith("@@"):
            # @@ -a,b +c,d @@
            try:
                plus = line.split("+", 1)[1]
                new_ln = int(plus.split(",")[0].split(" ")[0])
            except (IndexError, ValueError):
                new_ln = 0
            in_hunk = True
            continue
        if not in_hunk:
            continue                       # diff/index/--- +++ headers
        if line.startswith("\\"):
            continue                       # "\ No newline at end of file"
        if line.startswith("+"):
            body = line[1:]
            if not _is_noise(body, options):
                added.append(body)
                changed_new_lines.append(new_ln)
            new_ln += 1
        elif line.startswith("-"):
            body = line[1:]
            if not _is_noise(body, options):
                removed.append(body)
        else:
            new_ln += 1
    return added, removed, changed_new_lines


def scan_changes(
    cwd: Path,
    mode: str = "working",
    options: DiffOptions = DEFAULT_DIFF_OPTIONS,
) -> DiffScan:
    """Scan the repo for meaningful Java changes in ``mode``."""
    if not is_git_repo(cwd):
        raise GitError(f"{cwd} is not a git repository")

    flags = options.git_flags()
    paths, untracked, whitespace_only = _changed_java_paths(cwd, mode, options)
    scan = DiffScan(skipped_whitespace_only=list(whitespace_only))

    for path in paths:
        is_new = path in untracked
        if is_new:
            # Untracked files have no diff; synthesize one treating all lines as added.
            content = (cwd / path).read_text(encoding="utf-8", errors="replace")
            body = "".join(f"+{ln}\n" for ln in content.splitlines())
            diff_text = f"@@ -0,0 +1,{len(content.splitlines())} @@\n{body}"
        elif mode == "staged":
            diff_text = _run_git(["diff", "--cached", *flags, "--", path], cwd)
        else:
            diff_text = _run_git(["diff", *flags, "--", path], cwd)

        added, removed, changed = parse_hunks(diff_text, options)
        if not is_new and not added and not removed:
            # Everything git reported for this file was whitespace / blank lines.
            scan.skipped_whitespace_only.append(path)
            continue

        scan.diffs.append(FileDiff(
            path=path, diff_text=diff_text, added_lines=added, removed_lines=removed,
            changed_new_lines=changed, is_new_file=is_new,
        ))
    return scan


def get_file_diffs(
    cwd: Path, mode: str = "working", options: DiffOptions = DEFAULT_DIFF_OPTIONS
) -> List[FileDiff]:
    """Return per-file diffs for all meaningfully changed Java files in ``mode``."""
    return scan_changes(cwd, mode, options).diffs


# --------------------------------------------------------------------------- #
# MCP tool
# --------------------------------------------------------------------------- #

SCAN_SCHEMA = {
    "type": "object",
    "properties": {
        "project_dir": {"type": "string", "description": "git 저장소 루트"},
        "mode": {"type": "string", "enum": ["working", "staged"],
                 "description": "working=미스테이징 변경, staged=스테이징된 변경"},
        "ignore_whitespace": {"type": "boolean", "description": "공백만 바뀐 변경 무시 (기본 true)"},
        "ignore_blank_lines": {"type": "boolean", "description": "빈 줄 변경 무시 (기본 true)"},
    },
    "required": ["project_dir"],
}


def tool_scan_changes(args: dict) -> dict:
    options = DiffOptions(
        ignore_whitespace=bool(args.get("ignore_whitespace", True)),
        ignore_blank_lines=bool(args.get("ignore_blank_lines", True)),
    )
    scan = scan_changes(Path(args["project_dir"]), args.get("mode", "working"), options)
    return scan.to_dict()


def register(registry) -> None:
    registry.register(
        "git_scan_changes",
        "변경된 Java 파일의 diff를 조회합니다. 공백/빈 줄만 바뀐 파일은 제외하고 "
        "제외 목록을 함께 반환합니다.",
        SCAN_SCHEMA,
        tool_scan_changes,
    )
