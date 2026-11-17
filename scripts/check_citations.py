"""Check the `path:line` citations in a Markdown document against the working tree.

docs/security/THREAT_MODEL.md backs every claim with a citation such as
`src/pointcloud_detect/api/app.py:26-39`, and follows a path with shorthand for more
lines in the same file (`:83-86`). Code moves, and a citation that points at the wrong
lines is worse than none, because it looks like evidence. This reports every citation
whose file no longer exists or whose lines run past the end of the file.

    python scripts/check_citations.py docs/security/THREAT_MODEL.md

It cannot tell whether the cited lines still say what the text claims. For that,
--changed-since lists the citations whose lines were edited since a revision, so a
person can re-read exactly those, and --remap-from rewrites the line numbers of
citations whose lines are unchanged since that revision but have moved, leaving the
edited ones for that person. Both compare with a checkout of the revision, which this
script reads as files rather than running git:

    git worktree add --detach /tmp/old REV
    python scripts/check_citations.py docs/security/THREAT_MODEL.md --changed-since /tmp/old
    python scripts/check_citations.py docs/security/THREAT_MODEL.md --remap-from /tmp/old
    git worktree remove /tmp/old

What counts as a citation: a backticked span whose first path segment is a file or
directory at the repository root. That keeps `uploads/*` and `/api` out. Spans that
name a git revision (`origin/main:...`, `git show abc1234:...`) point outside the
working tree and are skipped, and a shorthand `:N` after one of them is not attributed
to the previous path.

The publication export (scripts/publication/prepare.sh) leaves some internal paths out
of the public repository, itself included. A citation of one of those (EXPORT_REMOVED)
is checked like any other where the export tooling is present, and skipped in an export,
where it is absent, so the same document passes in both trees.

Exit status is 1 if any citation is broken, so CI can run it.
"""

from __future__ import annotations

import argparse
import difflib
import glob
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SPAN = re.compile(r"`([^`\n]+)`")
# path, then optionally :start or :start-end
CITATION = re.compile(r"^(?P<path>[\w./*-]+?)(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?$")
SHORTHAND = re.compile(r"^:(?P<start>\d+)(?:-(?P<end>\d+))?$")
REVISION = re.compile(r"^(git |origin/|[0-9a-f]{7,40}:)")

# REMOVE_PATHS in scripts/publication/prepare.sh; tests/test_check_citations.py keeps the
# two in step. Its presence is what marks the internal tree.
EXPORT_TOOLING = "scripts/publication"
EXPORT_REMOVED = (
    "aidlc-docs",
    "docs/task-tracker.md",
    "docs/meeting-agenda.md",
    "scripts/publication",
    "docs/PUBLICATION.md",
    "docs/PUBLICATION_SIM_PACKAGE.md",
    ".kiro",
    ".gitleaksignore",
)


@dataclass
class Citation:
    line_no: int  # line in the Markdown document
    path: str
    start: int | None
    end: int | None
    col: int  # offset of the span in the document line, for rewriting
    text: str  # the span as written, without backticks

    def label(self) -> str:
        if self.start is None:
            return self.path
        rng = f"{self.start}" if self.end is None else f"{self.start}-{self.end}"
        return f"{self.path}:{rng}"


def repo_roots() -> set[str]:
    return {p.name for p in ROOT.iterdir()}


def parse(doc: Path) -> list[Citation]:
    roots = repo_roots()
    found: list[Citation] = []
    for n, line in enumerate(doc.read_text().splitlines(), start=1):
        last: str | None = None  # the path a `:N` shorthand refers to
        for m in SPAN.finditer(line):
            text = m.group(1).strip()
            if REVISION.match(text):
                last = None
                continue
            short = SHORTHAND.match(text)
            if short:
                if last is not None:
                    start, end = short.group("start"), short.group("end")
                    found.append(
                        Citation(
                            n,
                            last,
                            int(start),
                            int(end) if end else None,
                            m.start(1),
                            text,
                        )
                    )
                continue
            c = CITATION.match(text)
            if not c or c.group("path").split("/")[0] not in roots:
                last = None
                continue
            path, start, end = c.group("path"), c.group("start"), c.group("end")
            found.append(
                Citation(
                    n,
                    path,
                    int(start) if start else None,
                    int(end) if end else None,
                    m.start(1),
                    text,
                )
            )
            last = path
    return found


def line_count(path: Path) -> int:
    return len(path.read_text(errors="replace").splitlines())


def removed_by_export(path: str, root: Path = ROOT) -> bool:
    """Whether ``path`` is one the export leaves out, and this tree is an export."""
    if (root / EXPORT_TOOLING).exists():
        return False
    path = path.rstrip("/")
    return any(path == p or path.startswith(p + "/") for p in EXPORT_REMOVED)


def problems(c: Citation) -> str | None:
    target = ROOT / c.path.rstrip("/")
    if removed_by_export(c.path):
        return None
    if "*" in c.path:
        return None if glob.glob(str(ROOT / c.path)) else "no file matches"
    if not target.exists():
        return "file does not exist"
    if c.start is None:
        return None
    if target.is_dir():
        return "line numbers on a directory"
    end = c.end if c.end is not None else c.start
    if c.start < 1 or end < c.start:
        return "bad line range"
    total = line_count(target)
    if end > total:
        return f"past end of file ({total} lines)"
    return None


def old_lines(old_tree: Path, path: str) -> list[str] | None:
    """The file's lines in the checkout of the old revision, or None if it had none."""
    old_path = old_tree / path
    if not old_path.is_file():
        return None
    return old_path.read_text(errors="replace").splitlines()


def line_map(old_tree: Path, path: str) -> dict[int, int] | None:
    """Old line number -> new line number, for lines unchanged since old_tree."""
    old = old_lines(old_tree, path)
    new_path = ROOT / path
    if old is None or not new_path.is_file():
        return None
    new = new_path.read_text(errors="replace").splitlines()
    mapping: dict[int, int] = {}
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, _ in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                mapping[i1 + k + 1] = j1 + k + 1
    return mapping


def moved(c: Citation, mapping: dict[int, int]) -> tuple[int, int | None] | None:
    """The citation's new range, or None if any cited line was edited."""
    assert c.start is not None
    end = c.end if c.end is not None else c.start
    cited = range(c.start, end + 1)
    if not all(k in mapping for k in cited):
        return None
    new = [mapping[k] for k in cited]
    if new != list(range(new[0], new[0] + len(new))):
        return None  # lines inserted inside the range
    return new[0], (new[-1] if c.end is not None else None)


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("docs", nargs="+", type=Path)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--changed-since", metavar="OLD_TREE", type=Path)
    mode.add_argument("--remap-from", metavar="OLD_TREE", type=Path)
    args = ap.parse_args()

    status = 0
    for doc in args.docs:
        cites = parse(doc)
        old_tree = args.changed_since or args.remap_from
        if old_tree:
            if not old_tree.is_dir():
                print(f"{old_tree}: not a directory; pass a checkout of the revision")
                return 2
            maps: dict[str, dict[int, int] | None] = {}
            edits: dict[int, list[tuple[Citation, str]]] = {}
            for c in cites:
                if c.start is None:
                    continue
                if c.path not in maps:
                    maps[c.path] = line_map(old_tree, c.path)
                mapping = maps[c.path]
                if mapping is None:
                    print(
                        f"{doc}:{c.line_no}: {c.label()}: not in {old_tree} or gone; "
                        "check by hand"
                    )
                    continue
                new = moved(c, mapping)
                if new is None:
                    print(
                        f"{doc}:{c.line_no}: {c.label()}: cited lines edited since "
                        f"{old_tree}"
                    )
                elif args.remap_from and (new[0], new[1]) != (c.start, c.end):
                    rng = f"{new[0]}" if new[1] is None else f"{new[0]}-{new[1]}"
                    prefix = "" if c.text.startswith(":") else c.path
                    edits.setdefault(c.line_no, []).append((c, f"{prefix}:{rng}"))
            if args.remap_from and edits:
                lines = doc.read_text().splitlines(keepends=True)
                for n, changes in edits.items():
                    line = lines[n - 1]
                    for c, replacement in sorted(changes, key=lambda e: -e[0].col):
                        line = line[: c.col] + replacement + line[c.col + len(c.text) :]
                    lines[n - 1] = line
                doc.write_text("".join(lines))
                print(f"{doc}: moved {sum(len(v) for v in edits.values())} citations")

        broken = [(c, p) for c in cites if (p := problems(c))]
        for c, p in broken:
            print(f"{doc}:{c.line_no}: {c.label()}: {p}")
        if broken:
            status = 1
        print(f"{doc}: {len(cites)} citations, {len(broken)} broken")
    return status


if __name__ == "__main__":
    sys.exit(main())
