"""
Context repo access — the qa-context git repo holding AS-IS business rules.

master = production AS-IS. A ticket gets its own branch (e.g. TAT-1234) where the story
review edits rules.md: the branch's diff vs master is the story's rule change, merged like code. Writes never touch the working tree:
commits are built with git plumbing on a temporary index, so a user's checkout and
uncommitted edits in the context repo are left alone.
"""
import os
import re
import subprocess
import tempfile
from pathlib import Path

import config

MODULE_MAP_FILE = "_module-map.md"
# Files loaded into the review prompt, in this order. Missing files are skipped.
CONTEXT_FILES = ("README.md", "rules.md", "flows.md", "statuses.md",
                 "messages.md", "mismatches.md", "bugs.md", "open-questions.md")


class ContextRepoError(RuntimeError):
    """The context repo is missing or a git command against it failed."""


def _git(*args: str, repo: str | None = None, env: dict | None = None,
         input_text: str | None = None) -> str:
    repo = repo or config.CONTEXT_REPO_PATH
    if not Path(repo, ".git").exists():
        raise ContextRepoError(f"Not a git repo: {repo} (set CONTEXT_REPO_PATH)")
    r = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, input=input_text,
        env={**os.environ, **(env or {})},
    )
    if r.returncode != 0:
        raise ContextRepoError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def branch_exists(branch: str) -> bool:
    try:
        _git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        return True
    except ContextRepoError:
        return False


def read_file(path: str, ref: str | None = None) -> str | None:
    """Read a file at a git ref (default: the base branch). None if absent."""
    ref = ref or config.CONTEXT_BASE_BRANCH
    try:
        return _git("show", f"{ref}:{path}")
    except ContextRepoError:
        return None


# ── Module map ────────────────────────────────────────────────────────────────

def _split_cell(cell: str) -> list[str]:
    return [p.strip() for p in cell.split(",") if p.strip()]


def parse_module_map(text: str) -> list[dict]:
    """Parse the module table in _module-map.md into dicts."""
    modules = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 5 or cells[0] in ("Module", "") or set(cells[0]) <= {"-"}:
            continue
        modules.append({
            "id": cells[0],
            "folder": cells[1],
            "keywords": _split_cell(cells[2]),
            "be_paths": _split_cell(cells[3]),
            "fe_paths": _split_cell(cells[4]),
        })
    return modules


def load_module_map(ref: str | None = None) -> list[dict]:
    text = read_file(MODULE_MAP_FILE, ref)
    if text is None:
        raise ContextRepoError(f"{MODULE_MAP_FILE} not found on {ref or config.CONTEXT_BASE_BRANCH}")
    return parse_module_map(text)


def detect_modules(text: str, modules: list[dict]) -> list[dict]:
    """Modules whose keywords appear in the text, most keyword hits first."""
    low = text.lower()
    scored = []
    for m in modules:
        hits = [k for k in m["keywords"] if k.lower() in low]
        if hits:
            scored.append((len(hits), m, hits))
    scored.sort(key=lambda t: -t[0])
    return [{**m, "matched_keywords": hits} for _, m, hits in scored]


def path_in_module(path: str, prefixes: list[str]) -> bool:
    """True if path matches an include prefix and no `!` exclude prefix."""
    excludes = [p[1:] for p in prefixes if p.startswith("!")]
    includes = [p for p in prefixes if not p.startswith("!")]
    if any(path.startswith(e) for e in excludes):
        return False
    return any(path.startswith(i) for i in includes)


# ── Module context ────────────────────────────────────────────────────────────

def load_module_context(module: dict, ref: str | None = None) -> str:
    """Concatenate the module's context files at a ref, for an AI prompt."""
    parts = []
    for name in CONTEXT_FILES:
        text = read_file(f"{module['folder']}/{name}", ref)
        if text:
            parts.append(f"===== {module['folder']}/{name} =====\n{text.strip()}\n")
    return "\n".join(parts)


def next_rule_number(module: dict, ref: str | None = None) -> int:
    """Next free BR-<module>-NNN number. IDs are never reused, and stories in flight must not
    collide: without `ref`, every local and remote branch (master, story branches, env branches)
    is scanned, so two open stories never get the same number."""
    if ref:
        refs = [ref]
    else:
        refs = [r for r in _git("for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes").split()
                if not r.endswith("/HEAD")] or [config.CONTEXT_BASE_BRANCH]
    try:
        out = _git("grep", "-h", "-o", "-E", rf"BR-{re.escape(module['id'])}-[0-9]+", *refs,
                   "--", module["folder"])
    except ContextRepoError:  # git grep exits 1 when nothing matches
        out = ""
    nums = [int(n) for n in re.findall(rf"BR-{re.escape(module['id'])}-(\d+)", out)]
    return max(nums, default=0) + 1


# ── Rules as code: a story edits rules.md on its branch ─────────────────────────
# A story's rule changes are edits of modules/<m>/rules.md on its context branch, merged to env
# branches and to master like code: CHANGE rewrites an existing row, REMOVE strikes it through, ADD
# appends rows to the story's own "### <key> — <title>" section. Touched rows get status 🕓 and
# "<key> · <source>" as evidence. The diff vs master is the story's rule change.

RULE_ROW = re.compile(r"^\|\s*((?:BR|PL)-[A-Za-z0-9]+-\d+)\s*\|")
CHANGE_KINDS = ("ADD", "CHANGE", "REMOVE")


def rules_path(module: dict) -> str:
    return f"{module['folder']}/rules.md"


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |\n"


def rule_rows(text: str) -> dict[str, str]:
    """Rule ID → table row line."""
    rows = {}
    for line in text.splitlines():
        m = RULE_ROW.match(line)
        if m:
            rows[m.group(1)] = line
    return rows


def _table_header(text: str) -> list[str]:
    for line in text.splitlines():
        if line.startswith("|") and _cells(line)[0] == "ID":
            return _cells(line)
    return ["ID", "Rule", "Status", "Evidence"]


def apply_rule_changes(rules_text: str, changes: list[dict], issue_key: str,
                       story_title: str) -> tuple[str, list[str]]:
    """Apply ADD / CHANGE / REMOVE rows ({change, id, rule, source}) to a rules.md text.
    Returns (new text, notes about skipped changes). Re-applying a review updates the same rows."""
    lines = rules_text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    header = _table_header(rules_text)
    notes = []

    def find(rid):
        return next((i for i, ln in enumerate(lines)
                     if (m := RULE_ROW.match(ln)) and m.group(1) == rid), None)

    def section_end():
        """Index after the last row of the story's section; the section is created when missing."""
        start = next((i for i, ln in enumerate(lines)
                      if ln.startswith(f"### {issue_key} ") or ln.rstrip() == f"### {issue_key}"), None)
        if start is None:
            if lines and lines[-1].strip():
                lines.append("\n")
            lines.extend([f"### {issue_key} — {story_title}".rstrip(" —") + "\n",
                          _row(header), _row(["---"] * len(header))])
            return len(lines)
        i = start + 1
        while i < len(lines) and lines[i].startswith("|"):
            i += 1
        return i

    for c in changes:
        kind, rid, text = c["change"].upper(), c["id"], c["rule"]
        evidence = f"{issue_key} · {c.get('source') or 'story'}"
        idx = find(rid)
        if kind == "ADD" and idx is not None and issue_key not in _cells(lines[idx])[-1]:
            notes.append(f"Skipped ADD {rid}: the ID already exists in rules.md")
            continue
        if idx is None and kind != "ADD":
            notes.append(f"Skipped {kind} {rid}: not in rules.md")
            continue
        if idx is not None:  # CHANGE, REMOVE, or a re-review updating its own ADD
            cells = _cells(lines[idx])
            if kind == "REMOVE":
                cells[1] = f"~~{cells[1].strip('~')}~~ Removed by {issue_key}: {text}"
            else:
                cells[1] = text
            cells[-2], cells[-1] = "🕓", evidence
            lines[idx] = _row(cells)
            continue
        lines.insert(section_end(), _row([rid, text] + [""] * (len(header) - 4) + ["🕓", evidence]))
    return "".join(lines), notes


def rule_changes(module: dict, ref: str, base: str | None = None) -> list[dict]:
    """Rule rows that differ between base (default master) and ref: [{change, id, row, base_row}]."""
    before = rule_rows(read_file(rules_path(module), base) or "")
    after = rule_rows(read_file(rules_path(module), ref) or "")
    out = []
    for rid, row in after.items():
        if rid not in before:
            out.append({"change": "ADD", "id": rid, "row": row, "base_row": None})
        elif row != before[rid]:
            kind = "REMOVE" if _cells(row)[1].startswith("~~") else "CHANGE"
            out.append({"change": kind, "id": rid, "row": row, "base_row": before[rid]})
    out += [{"change": "REMOVE", "id": rid, "row": None, "base_row": row}
            for rid, row in before.items() if rid not in after]
    return out


def rule_changes_block(module: dict, ref: str, base: str | None = None) -> str:
    """The branch's rule changes vs base as a markdown list, for prompts. Empty when none."""
    lines = []
    for c in rule_changes(module, ref, base):
        line = f"- {c['change']} {c['id']}: {_cells(c['row'])[1] if c['row'] else '(row deleted)'}"
        if c["base_row"] and c["change"] != "ADD":
            line += f"\n  was: {_cells(c['base_row'])[1]}"
        lines.append(line)
    return "\n".join(lines)


def load_story_rules(issue_key: str, text: str, max_modules: int = 3) -> tuple[list[dict], str, str]:
    """Rules for test design, read only from the ticket's context branch <issue_key> (created by
    the story review from the base branch): its rules.md (AS-IS + the story's edits) and the list
    of rule changes vs the base branch. Modules = those whose rules the story changes, plus those
    the story text matches. Returns (modules, prompt block, error); error is set when there is
    nothing to use. Never raises."""
    try:
        if not branch_exists(issue_key):
            return [], "", (f"Context branch {issue_key} not found — run 'Review userstory' first, "
                            f"it creates the branch with the story's rule changes.")
        all_modules = load_module_map(issue_key)
        planned = {}
        for m in all_modules:
            txt = rule_changes_block(m, issue_key)
            if txt:
                planned[m["id"]] = txt
        detected = detect_modules(text, all_modules)[:max_modules]
        ids = list(planned) + [m["id"] for m in detected if m["id"] not in planned]
        modules = [m for m in all_modules if m["id"] in ids]
        blocks = []
        for m in modules:
            block = f"##### MODULE {m['id']} — AS-IS rules (branch {issue_key})\n\n{load_module_context(m, issue_key)}"
            if m["id"] in planned:
                block += (f"\n===== Rule changes of {issue_key} vs production (🕓) =====\n"
                          f"{planned[m['id']].strip()}\n")
            blocks.append(block)
    except ContextRepoError as exc:
        return [], "", f"Context repo unavailable: {exc}"
    if not modules:
        return [], "", f"No module matched on context branch {issue_key} — check _module-map.md keywords."
    return modules, "\n\n".join(blocks), ""


# ── Write: commit one file to a branch without a checkout ──────────────────────

def commit_file_to_branch(branch: str, path: str, content: str, message: str,
                          base: str | None = None) -> str:
    """Create/update `path` on `branch` (created from `base` if missing). Returns commit sha.

    Uses a temporary index so the working tree and the real index are untouched — except when
    `branch` is checked out, where only `path` is refreshed afterwards.
    """
    base = base or config.CONTEXT_BASE_BRANCH
    checked_out = _git("rev-parse", "--abbrev-ref", "HEAD").strip() == branch
    if checked_out and _git("status", "--porcelain", "--", path).strip():
        raise ContextRepoError(
            f"{path} has uncommitted changes on checked-out branch {branch} in "
            f"{config.CONTEXT_REPO_PATH}; commit or discard them first."
        )
    exists = branch_exists(branch)
    parent = branch if exists else base
    parent_sha = _git("rev-parse", parent).strip()
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp, "index"))}
        _git("read-tree", parent_sha, env=env)
        blob = _git("hash-object", "-w", "--stdin", input_text=content).strip()
        _git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
        tree = _git("write-tree", env=env).strip()
    sha = _git("commit-tree", tree, "-p", parent_sha, "-m", message).strip()
    # Old value guards against a concurrent commit to the same branch.
    _git("update-ref", f"refs/heads/{branch}", sha, parent_sha if exists else "0" * 40)
    if checked_out:
        # HEAD now points at the new commit; sync just this file so the checkout isn't stale.
        _git("checkout", "HEAD", "--", path)
    return sha
