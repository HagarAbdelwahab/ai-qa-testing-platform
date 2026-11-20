"""
Reconciliation Agent — runs AFTER development, once test cases are written.

Answers: do the story's test cases cover everything the code changed, plus the AS-IS
rules the change touches (regression)? Compares three sources per module:
  rules  — AS-IS rules (context master) + the ticket's rule changes (rules.md on context branch <ticket> vs master)
  code   — diff of the ticket's dev branch(es) vs the production branch, BE and FE
  tests  — AIO cases linked to the story; a case names its rules in the title, e.g.
           "[BR-CD-021] Change response after rejection"

Read-only on the code repos: only `git fetch` (remote refs) and `git diff/log` are run.
"""
import json
import re
import subprocess
from pathlib import Path

import config
import context_repo

RULE_ID_RE = re.compile(r"\bBR-[A-Z0-9]+-\d+\b")
SKIP_DIFF_RE = re.compile(r"(/migrations/|\.lock$|package-lock\.json$|poetry\.lock$|\.min\.js$|\.snap$)")
MAX_DIFF_CHARS = 120_000
MAX_FILE_DIFF_LINES = 400


class ReconcileError(RuntimeError):
    """A required source (code repo, branch, AIO) could not be read."""


def _git(repo: str, *args: str) -> str:
    if not Path(repo, ".git").exists():
        raise ReconcileError(f"Not a git repo: {repo}")
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise ReconcileError(f"git {' '.join(args)} in {repo} failed: {r.stderr.strip()}")
    return r.stdout


# ── Code side ─────────────────────────────────────────────────────────────────

def branch_matches_key(branch: str, issue_key: str) -> bool:
    """True if the branch name carries the Jira key, e.g. feature/TAT-123-x or tat-123-x."""
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(issue_key)}(?![0-9])", branch, re.I) is not None


def find_dev_branches(repo: str, issue_key: str) -> list[str]:
    refs = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin").split()
    return sorted(r for r in refs if r != "origin/HEAD" and branch_matches_key(r, issue_key))


PR_RE = re.compile(r"^(?:.*/pullrequest/)?#?(\d+)/?$", re.I)


def parse_attached_refs(raw: str) -> list[str]:
    """Split a comma/space/newline separated list of branches, PR numbers or PR URLs."""
    return [t for t in re.split(r"[,\s]+", raw or "") if t]


def resolve_attached_ref(repo: str, item: str) -> tuple[str, str]:
    """(label, git ref) for a branch name, PR number (123 / #123) or Azure PR URL (…/pullrequest/123).

    A PR resolves to its source-branch tip: the 2nd parent of origin's refs/pull/<id>/merge,
    fetched into FETCH_HEAD only (no named ref is created in the repo).
    """
    m = PR_RE.match(item)
    if m:
        pr = m.group(1)
        try:
            _git(repo, "fetch", "-q", "origin", f"refs/pull/{pr}/merge")
        except ReconcileError:
            raise ReconcileError(f"PR {pr} not found on origin of {repo} (no refs/pull/{pr}/merge). "
                                 f"Check the PR belongs to this repo, or attach its source branch instead.")
        sha = _git(repo, "rev-parse", "FETCH_HEAD^2").strip()
        subject = _git(repo, "log", "-1", "--format=%s", "FETCH_HEAD").strip()
        src = re.search(r"\bfrom (\S+) into ", subject)
        return f"PR {pr}" + (f" ({src.group(1)})" if src else ""), sha
    branch = item if item.startswith("origin/") else f"origin/{item}"
    try:
        _git(repo, "rev-parse", "--verify", "-q", f"{branch}^{{commit}}")
    except ReconcileError:
        raise ReconcileError(f"Branch {branch} not found in {repo}. Check the name, or fetch the remote.")
    return branch, branch


def changed_files(repo: str, base: str, branch: str) -> list[tuple[str, str]]:
    """(status, path) for files changed on branch since it left base (three-dot diff)."""
    out = _git(repo, "diff", "--name-status", f"origin/{base}...{branch}")
    files = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            files.append((parts[0][0], parts[-1]))   # rename R100 old new -> new path
    return files


def commit_log(repo: str, base: str, branch: str) -> list[str]:
    return _git(repo, "log", "--format=%h %ad %s", "--date=short",
                f"origin/{base}..{branch}").splitlines()


def file_diff(repo: str, base: str, branch: str, path: str) -> str:
    text = _git(repo, "diff", f"origin/{base}...{branch}", "--", path)
    lines = text.splitlines()
    if len(lines) > MAX_FILE_DIFF_LINES:
        lines = lines[:MAX_FILE_DIFF_LINES] + [f"... ({len(lines) - MAX_FILE_DIFF_LINES} more lines truncated)"]
    return "\n".join(lines)


def group_by_module(paths: list[str], modules: list[dict], side: str) -> dict[str, list[str]]:
    """Group changed paths per module ID using the module map's BE/FE prefixes.

    A path can belong to several modules. Paths in no module go under "_unmapped".
    """
    key = "be_paths" if side == "BE" else "fe_paths"
    groups: dict[str, list[str]] = {}
    for p in paths:
        owners = [m["id"] for m in modules if context_repo.path_in_module(p, m[key])]
        for o in owners or ["_unmapped"]:
            groups.setdefault(o, []).append(p)
    return groups


def env_files_in_scope(files: list[tuple[str, str]], modules: list[dict], side: str,
                       scope: set[str]) -> tuple[list[tuple[str, str]], int]:
    """Keep env-branch files that fall in a scope module. Returns (kept, dropped count).

    An env branch carries every story merged there, so its diff is cut to the story's
    modules. No scope -> nothing can be cut, everything is kept.
    """
    if not scope:
        return files, 0
    groups = group_by_module([p for _, p in files], modules, side)
    keep = {p for mid, paths in groups.items() if mid in scope for p in paths}
    kept = [f for f in files if f[1] in keep]
    return kept, len(files) - len(kept)


def collect_code_changes(issue_key: str, modules: list[dict], attached: dict | None = None,
                         environment: str = "", scope: set[str] | None = None) -> dict:
    """Branches, commits, changed files per module and a size-capped diff, for BE and FE.

    attached: {"BE": [...], "FE": [...]} branches / PRs added by the user on top of the
    branches found by ticket key (for work whose branch name lacks the key).
    environment: testing-env branch (e.g. qa-squad-a) diffed vs base on both sides, for work
    with no key at all; only files in the scope modules are kept.
    """
    base = config.CODE_BASE_BRANCH
    attached = attached or {}
    scope = scope or set()
    result = {"sides": {}, "diff": "", "truncated": False}
    budget = MAX_DIFF_CHARS
    for side, repo in (("BE", config.BE_REPO_PATH), ("FE", config.FE_REPO_PATH)):
        if config.RECONCILE_FETCH:
            print(f"  Fetching {side} remote...")
            _git(repo, "fetch", "--prune", "origin")
        refs = [(b, b) for b in find_dev_branches(repo, issue_key)]
        for item in attached.get(side, []):
            label, ref = resolve_attached_ref(repo, item)
            if all(r != ref for _, r in refs):
                refs.append((label, ref))
                print(f"  {side}: attached {label}")
        env_ref = None
        if environment:
            env_ref = f"origin/{environment}"
            try:
                _git(repo, "rev-parse", "--verify", "-q", f"{env_ref}^{{commit}}")
            except ReconcileError:
                raise ReconcileError(f"Environment branch {env_ref} not found in {repo}.")
            refs.append((f"env {environment}", env_ref))
        side_info = {"repo": repo, "branches": [label for label, _ in refs], "commits": [], "files": [],
                     "groups": {}, "env_dropped": 0}
        for br, ref in refs:
            files = changed_files(repo, base, ref)
            if ref == env_ref:
                files, side_info["env_dropped"] = env_files_in_scope(files, modules, side, scope)
                print(f"  {side}: env {environment} — {len(files)} file(s) in story modules, "
                      f"{side_info['env_dropped']} out of scope")
            else:
                # Env commits mix every story merged there; listing them adds noise, not evidence.
                side_info["commits"] += [f"{br}: {c}" for c in commit_log(repo, base, ref)]
            for status, path in files:
                side_info["files"].append((status, path, br, ref))
        paths = sorted({p for _, p, _, _ in side_info["files"]})
        side_info["groups"] = group_by_module(paths, modules, side)
        diffed = set()
        for status, path, br, ref in side_info["files"]:
            if SKIP_DIFF_RE.search(path) or path in diffed:
                continue
            diffed.add(path)
            chunk = f"\n##### {side} {status} {path} ({br})\n{file_diff(repo, base, ref, path)}\n"
            if len(chunk) > budget:
                result["truncated"] = True
                continue
            result["diff"] += chunk
            budget -= len(chunk)
        result["sides"][side] = side_info
    return result


# ── Test side ─────────────────────────────────────────────────────────────────

def rule_ids_in(text: str) -> list[str]:
    return sorted(set(RULE_ID_RE.findall(text or "")))


def tag_cases(cases: list[dict]) -> list[dict]:
    return [{**c, "rule_ids": rule_ids_in(c.get("title", ""))} for c in cases]


def coverage_precheck(rule_ids: set[str], cases: list[dict]) -> dict:
    """Deterministic link check before the AI reads the content."""
    covered = {r for c in cases for r in c["rule_ids"]}
    return {
        "rules_without_case": sorted(rule_ids - covered),
        "cases_without_rule": [c["key"] for c in cases if not c["rule_ids"]],
        "cases_with_unknown_rule": [
            f"{c['key']} → {', '.join(sorted(set(c['rule_ids']) - rule_ids))}"
            for c in cases if set(c["rule_ids"]) - rule_ids
        ],
    }


# ── Orchestration ─────────────────────────────────────────────────────────────

def _fmt_list(items: list[str], empty: str = "(none)") -> str:
    return "\n".join(f"- {i}" for i in items) if items else empty


CASES_BLOCK_RE = re.compile(r"```cases-json\s*\n(.*?)```", re.S)
_CASE_FIELDS = ("title", "precondition", "step", "test_data", "expected_result")


def split_cases(report: str) -> tuple[str, list[dict]]:
    """Strip the ```cases-json block from the report and return (report, cases).
    Cases missing a title or expected result are dropped; a malformed block yields no cases."""
    m = CASES_BLOCK_RE.search(report)
    if not m:
        return report, []
    report = (report[:m.start()] + report[m.end():]).rstrip()
    try:
        raw = json.loads(m.group(1))
    except ValueError:
        return report, []
    cases = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        case = {f: str(c.get(f) or "").strip() for f in _CASE_FIELDS}
        if case["title"] and case["expected_result"]:
            case["row"] = c.get("row")
            cases.append(case)
    return report, cases


def generate_reconciliation(issue_key: str, be_refs: str = "", fe_refs: str = "",
                            environment: str = "") -> str:
    from jira_client import get_issue, adf_to_text
    from test_generator import _call_ai
    import aio_client

    if not environment:
        raise ReconcileError("Environment is required (e.g. qa-squad-a).")

    print(f"  [1/5] Reading story: {issue_key}")
    issue = get_issue(issue_key)
    fields = issue["fields"]
    story_title = fields["summary"]
    description = adf_to_text(fields.get("description")) if fields.get("description") else ""

    print(f"  [2/5] Loading rules from context repo...")
    all_modules = context_repo.load_module_map()
    ticket_ref = issue_key if context_repo.branch_exists(issue_key) else None
    planned_by_module = {}
    for m in all_modules:
        if ticket_ref:
            txt = context_repo.rule_changes_block(m, ticket_ref)
            if txt:
                planned_by_module[m["id"]] = txt
    print(f"  Context branch {issue_key}: {'found' if ticket_ref else 'not found — AS-IS rules only'}")

    scope = set()
    if environment:
        detected = context_repo.detect_modules(f"{story_title}\n{description}", all_modules)
        scope = set(planned_by_module) | {m["id"] for m in detected}
        print(f"  Env {environment}: story modules {', '.join(sorted(scope)) or 'none matched — full env diff kept'}")

    print(f"  [3/5] Collecting code changes (base: {config.CODE_BASE_BRANCH})...")
    attached = {"BE": parse_attached_refs(be_refs), "FE": parse_attached_refs(fe_refs)}
    code = collect_code_changes(issue_key, all_modules, attached, environment, scope)
    for side, info in code["sides"].items():
        print(f"  {side}: {len(info['branches'])} branch(es), {len({f[1] for f in info['files']})} file(s)")
    if not any(info["branches"] for info in code["sides"].values()):
        raise ReconcileError(
            f"No dev branch containing {issue_key} found in BE or FE. "
            f"Check the environment, or attach the BE/FE branch or PR (number or URL), and run again.")

    touched = sorted({mid for info in code["sides"].values() for mid in info["groups"] if mid != "_unmapped"}
                     | set(planned_by_module))
    modules = [m for m in all_modules if m["id"] in touched]

    print(f"  [4/5] Reading test cases from AIO...")
    cases = tag_cases(aio_client.get_test_cases_for_story(config.JIRA_PROJECT_ID, int(issue["id"])))
    print(f"  {len(cases)} test case(s) linked to {issue_key}")

    rules_block, all_rule_ids = [], set()
    for m in modules:
        asis = context_repo.read_file(f"{m['folder']}/rules.md") or "(no rules.md)"
        planned = planned_by_module.get(m["id"], "(no rule changes)")
        all_rule_ids |= set(rule_ids_in(asis)) | set(rule_ids_in(planned))
        rules_block.append(f"##### MODULE {m['id']} — AS-IS rules (master)\n{asis}\n\n"
                           f"##### MODULE {m['id']} — rule changes of {issue_key} "
                           f"(rules.md on branch {issue_key} vs master)\n{planned}\n")
    precheck = coverage_precheck(all_rule_ids, cases)

    files_block = []
    for side, info in code["sides"].items():
        files_block.append(f"{side} branches: {', '.join(info['branches']) or '(none)'}")
        if info["env_dropped"]:
            files_block.append(f"  {side} · env {environment}: {info['env_dropped']} file(s) outside the story's "
                               f"modules ({', '.join(sorted(scope))}) were left out")
        for mid, paths in info["groups"].items():
            files_block.append(f"  {side} · {mid}: {', '.join(paths)}")
    commits = [c for info in code["sides"].values() for c in info["commits"]]
    cases_block = "\n".join(f"- {c['key']}: {c['title']}" for c in cases) or "(no test cases linked)"

    env_note = (f"Environment branch `{environment}` is diffed vs {config.CODE_BASE_BRANCH}. It holds EVERY story "
                f"merged to that env, not only {issue_key}: judge only changes that relate to this story, and list "
                f"the rest under Unmapped/unrelated changes instead of flagging them.\n") if environment else ""

    print(f"  [5/5] Reconciling with AI...")
    prompt = f"""You are a senior QA engineer doing the RECONCILIATION step after development.
Goal: decide whether the story's test cases cover (a) every behaviour the code change introduces
and (b) the AS-IS rules the change touches (regression). Judge from the evidence below only.
Never guess: if the diff does not show something, say so.

Story: {issue_key} — {story_title}
--- STORY DESCRIPTION ---
{description or "(no description)"}

--- RULES (per module) ---
Markers: ☐ built · ☑ approved · 🕓 planned by this ticket · ❓ open · ⚠ suspected bug
{chr(10).join(rules_block) or "(no module touched)"}

--- CODE CHANGES ---
{env_note}{chr(10).join(files_block)}
Commits:
{_fmt_list(commits)}
Diff (base {config.CODE_BASE_BRANCH}{", TRUNCATED — some files omitted" if code["truncated"] else ""}):
{code["diff"] or "(empty)"}

--- TEST CASES (rule IDs are tagged in titles) ---
{cases_block}

--- DETERMINISTIC LINK CHECK ---
Rules with no case tagged: {", ".join(precheck["rules_without_case"]) or "none"}
Cases with no rule tag: {", ".join(precheck["cases_without_rule"]) or "none"}
Cases tagging unknown rule IDs: {"; ".join(precheck["cases_with_unknown_rule"]) or "none"}
(Not every AS-IS rule needs a case — only rules the change touches or the story plans.)

AUDIENCE: a manual QA engineer who does NOT read code. Keep every code reference, but never leave one
unexplained: next to each code change, say in plain business language what it means for the user or the
business — what they can now do, see, or are prevented from doing, and what could go wrong. Only the test
cases listed above count as coverage; developer unit tests do not.

Produce the report in exactly this markdown format:

## Reconciliation — {issue_key}

**Verdict:** READY TO EXECUTE / GAPS FOUND / BLOCKED
[One sentence why, in business terms.]

### Scope
| Side | Branches | Files changed | Modules |
|---|---|---|---|

### What changed for the business
[3–6 bullets: the business behaviour this change introduces or alters, per business area.]

### Reconciliation table
| # | Status | Rule ID | Code change (file:line from diff) | Business impact | Test case(s) | Action |
|---|---|---|---|---|---|---|
Status is exactly one of:
✅ Covered (rule + code + test) · ⚠ Code without rule · ⚠ Code without test · ⚠ Rule without code ·
⚠ Behaviour differs from AC · ⚠ Orphan test · 🔁 Touched existing rule
Business impact: 1–2 plain sentences — what this code change does for the user/business and, for a ⚠ or 🔁
row, why the status matters (e.g. "Users now see their search words highlighted in help suggestions; no
rule says this should happen, so nobody has confirmed it is wanted or tested it").
Action: None / Ask PO → add rule + case / Add case / Ask dev / Possible bug / Fix link or remove / Add to regression scope.
One row per planned rule, per touched AS-IS rule, per behaviour change in the diff without a rule, and per orphan case.

### Status legend
[Only for statuses used in the table, one line each in plain words, e.g. "⚠ Code without rule — the
system does something new that no agreed business rule describes".]

### Regression scope
[AS-IS rule IDs touched by the diff (🔁), what existing user behaviour could break, and whether a case covers each.]

### Cases to add
[Numbered: title in the form "[BR-…] …" and, in business terms, what it must check.]

Then, right after that list, repeat the same cases (same titles, same order) as ONE fenced block
labelled cases-json, holding a JSON array. Each item: {{"title": "...", "precondition": "...",
"step": "numbered manual steps, one per line", "test_data": "...", "expected_result": "...",
"row": <table row #>}}. Manual business test cases only — what a QA engineer does and checks in the
UI/system, never unit tests, code or tooling. Write [] if there are no cases to add.

### Possible bugs — steps to reproduce
[One block per table row whose Action is "Possible bug", so a manual QA can reproduce it before reporting.
Business language only — no file names or code. If none, write "None."]
**Row <#> — <one-line title>**
- **Where:** screen / feature and user role
- **Preconditions:** account, data and state needed
- **Steps to reproduce:**
  1. …
  2. …
- **Expected:** per the rule / AC (cite the Rule ID or AC)
- **Actual (from the code change):** what the system will do instead, in business terms
- **Unknown:** any detail the evidence does not give — never invent it

### Questions for PO / dev
[Numbered, in business language, each tied to a row of the table.]

### Unmapped / unrelated changed files
[Files outside every module in the module map, or env-branch changes unrelated to this story — each with
its business impact in plain words and whether it matters for testing.]"""

    report = _call_ai(prompt, max_tokens=12000)
    print(f"  Reconciliation complete.")
    return report
