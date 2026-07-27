"""Git-based change detection.

Two modes map to the spec's local vs. staging distinction:

* ``working``  - changes in the working tree that have NOT been staged yet
                 (``codetest run`` / ``codetest generate``).
* ``staged``   - changes that have been promoted to the staging area
                 (``codetest run --stage``).

Formatting-only edits are noise for a test generator, so by default the diff
is taken with whitespace and blank-line changes ignored (:class:`DiffOptions`).
A file whose entire diff is whitespace/newline churn is dropped from the scan
instead of producing a pointless test.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiffOptions:
    """How aggressively to filter noise out of the diff.

    ``ignore_whitespace``  - 들여쓰기/정렬 등 공백만 바뀐 변경 무시 (git ``-w``).
    ``ignore_blank_lines`` - 의미 없는 빈 줄 추가/삭제 무시 (git ``--ignore-blank-lines``).
    """

    ignore_whitespace: bool = True
    ignore_blank_lines: bool = True

    def git_flags(self) -> List[str]:
        flags: List[str] = []
        if self.ignore_whitespace:
            flags += ["--ignore-all-space", "--ignore-space-at-eol"]
        if self.ignore_blank_lines:
            flags.append("--ignore-blank-lines")
        return flags


DEFAULT_DIFF_OPTIONS = DiffOptions()


@dataclass
class FileDiff:
    path: str            # repo-relative
    diff_text: str
    added_lines: List[str]
    removed_lines: List[str]
    changed_new_lines: List[int]   # line numbers in the NEW version of the file
    is_new_file: bool


@dataclass
class DiffScan:
    """Result of one scan: the meaningful diffs plus what was filtered out."""

    diffs: List[FileDiff] = field(default_factory=list)
    skipped_whitespace_only: List[str] = field(default_factory=list)
    options: DiffOptions = DEFAULT_DIFF_OPTIONS


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
    if options.ignore_blank_lines and not line.strip():
        return True
    return False


def _parse_hunks(
    diff_text: str, options: DiffOptions = DEFAULT_DIFF_OPTIONS
) -> tuple[List[str], List[str], List[int]]:
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
    scan = DiffScan(options=options, skipped_whitespace_only=list(whitespace_only))

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

        added, removed, changed = _parse_hunks(diff_text, options)
        if not is_new and not added and not removed:
            # Everything git reported for this file was whitespace / blank lines.
            scan.skipped_whitespace_only.append(path)
            continue

        scan.diffs.append(
            FileDiff(
                path=path,
                diff_text=diff_text,
                added_lines=added,
                removed_lines=removed,
                changed_new_lines=changed,
                is_new_file=is_new,
            )
        )
    return scan


def get_file_diffs(
    cwd: Path,
    mode: str = "working",
    options: DiffOptions = DEFAULT_DIFF_OPTIONS,
) -> List[FileDiff]:
    """Return per-file diffs for all meaningfully changed Java files in ``mode``."""
    return scan_changes(cwd, mode, options).diffs
