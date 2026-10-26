"""
Unit tests for the context-grounded story review and the reconciliation step:
  1. Module map parsing, keyword detection, path ownership
  2. Committing planned rules to a ticket branch WITHOUT touching the working tree
  3. Planned-rules block extraction from the review
  4. Dev-branch discovery, changed files, module grouping, rule-tag coverage check

Run with:
  cd testing-platform/backend && python -m pytest tests/test_context_and_reconcile.py -v
"""
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "agent"))

import context_repo  # noqa: E402
import reconciler  # noqa: E402
import story_reviewer  # noqa: E402

MODULE_MAP = """# Module map

| Module | Folder | Keywords | BE paths | FE paths |
|---|---|---|---|---|
| CD | modules/cash-division | قسمة الأموال النقدية, cash division, IBAN | project/apps/division_v2/, !project/apps/division_v2/providers/cma_transfer/ | src/views/v2/division/ |
| INV | modules/investment | investment division | project/apps/division_v2/providers/cma_transfer/ | src/views/v2/investment/ |
"""

RULES = """# rules
| BR-CD-001 | rule one | ☐ |
| BR-CD-055 | rule fifty-five | ☐ |
"""


def _run(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _commit_all(repo, msg="c"):
    _run(repo, "add", "-A")
    _run(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)


@pytest.fixture
def ctx_repo(tmp_path, monkeypatch):
    repo = tmp_path / "context"
    repo.mkdir()
    _run(repo, "init", "-q", "-b", "master")
    (repo / "_module-map.md").write_text(MODULE_MAP)
    (repo / "modules/cash-division").mkdir(parents=True)
    (repo / "modules/cash-division/rules.md").write_text(RULES)
    (repo / "modules/cash-division/README.md").write_text("# Cash division\n")
    _commit_all(repo)
    monkeypatch.setattr(context_repo.config, "CONTEXT_REPO_PATH", str(repo), raising=False)
    monkeypatch.setattr(context_repo.config, "CONTEXT_BASE_BRANCH", "master", raising=False)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    return repo


# ── 1. Module map ─────────────────────────────────────────────────────────────

class TestModuleMap:
    def test_parses_rows_and_skips_header(self):
        mods = context_repo.parse_module_map(MODULE_MAP)
        assert [m["id"] for m in mods] == ["CD", "INV"]
        assert mods[0]["folder"] == "modules/cash-division"
        assert "IBAN" in mods[0]["keywords"]

    def test_detects_arabic_keyword_case_insensitive(self):
        mods = context_repo.parse_module_map(MODULE_MAP)
        hit = context_repo.detect_modules("تعديل على قسمة الأموال النقدية و iban", mods)
        assert [m["id"] for m in hit] == ["CD"]
        assert set(hit[0]["matched_keywords"]) == {"قسمة الأموال النقدية", "IBAN"}

    def test_no_match_returns_empty(self):
        mods = context_repo.parse_module_map(MODULE_MAP)
        assert context_repo.detect_modules("profile mobile number", mods) == []

    def test_exclude_prefix_wins(self):
        prefixes = ["project/apps/division_v2/", "!project/apps/division_v2/providers/cma_transfer/"]
        assert context_repo.path_in_module("project/apps/division_v2/views.py", prefixes)
        assert not context_repo.path_in_module(
            "project/apps/division_v2/providers/cma_transfer/fsm.py", prefixes)


# ── 2. Context repo writes ────────────────────────────────────────────────────

class TestContextRepoWrites:
    def test_next_rule_number_continues_after_max(self, ctx_repo):
        mod = context_repo.load_module_map()[0]
        assert context_repo.next_rule_number(mod) == 56

    def test_commit_creates_branch_without_touching_worktree(self, ctx_repo):
        (ctx_repo / "scratch.txt").write_text("uncommitted user edit")
        sha = context_repo.commit_file_to_branch(
            "TAT-1", "modules/cash-division/planned/TAT-1.md", "| BR-CD-056 | x | 🕓 |\n", "planned")
        assert context_repo.branch_exists("TAT-1")
        assert context_repo.read_file("modules/cash-division/planned/TAT-1.md", "TAT-1").startswith("| BR-CD-056")
        # master, the checkout and the user's uncommitted file are untouched
        assert context_repo.read_file("modules/cash-division/planned/TAT-1.md") is None
        assert not (ctx_repo / "modules/cash-division/planned").exists()
        assert (ctx_repo / "scratch.txt").read_text() == "uncommitted user edit"
        assert len(sha) == 40

    def test_second_commit_updates_same_branch(self, ctx_repo):
        p = "modules/cash-division/planned/TAT-1.md"
        context_repo.commit_file_to_branch("TAT-1", p, "v1\n", "first")
        context_repo.commit_file_to_branch("TAT-1", p, "v2\n", "second")
        assert context_repo.read_file(p, "TAT-1") == "v2\n"
        # earlier files on the branch are kept
        assert context_repo.read_file("modules/cash-division/rules.md", "TAT-1") == RULES

    def test_refuses_branch_that_is_checked_out(self, ctx_repo):
        _run(ctx_repo, "checkout", "-q", "-b", "TAT-2")
        with pytest.raises(context_repo.ContextRepoError, match="checked out"):
            context_repo.commit_file_to_branch("TAT-2", "a.md", "x", "m")

    def test_missing_repo_raises_context_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(context_repo.config, "CONTEXT_REPO_PATH", str(tmp_path / "nope"), raising=False)
        with pytest.raises(context_repo.ContextRepoError):
            context_repo.load_module_map()


# ── 3. Story review ───────────────────────────────────────────────────────────

class TestStoryReviewPlannedRules:
    def test_extracts_block_per_module(self):
        review = (
            "text\n```planned-rules CD\n| Rule ID | Rule | Status | Source |\n|---|---|---|---|\n"
            "| BR-CD-056 | new rule | 🕓 | AC1 |\n```\nmore\n"
            "```planned-rules INV\n| BR-INV-010 | other | 🕓 | AC2 |\n```"
        )
        out = story_reviewer.extract_planned_rules(review)
        assert set(out) == {"CD", "INV"}
        assert "BR-CD-056" in out["CD"]

    def test_block_without_module_id_is_ignored(self):
        assert story_reviewer.extract_planned_rules("```planned-rules\n| x |\n```") == {}

    def test_load_context_falls_back_when_repo_missing(self):
        with patch.object(context_repo, "load_module_map",
                          side_effect=context_repo.ContextRepoError("no repo")):
            modules, block, warning = story_reviewer._load_context("cash division")
        assert modules == [] and block == ""
        assert "without AS-IS rules" in warning

    def test_load_context_includes_next_rule_id(self, ctx_repo):
        modules, block, warning = story_reviewer._load_context("change to cash division IBAN")
        assert [m["id"] for m in modules] == ["CD"]
        assert "Next free rule ID: BR-CD-056" in block
        assert "rule fifty-five" in block
        assert warning == ""


# ── 4. Reconciliation ─────────────────────────────────────────────────────────

class TestReconcileHelpers:
    @pytest.mark.parametrize("branch,expected", [
        ("origin/feature/TAT-123-iban", True),
        ("origin/tat-123-fix", True),
        ("origin/epic/TAT-123", True),
        ("origin/feature/TAT-1234-other", False),
        ("origin/feature/XTAT-123", False),
    ])
    def test_branch_matches_key(self, branch, expected):
        assert reconciler.branch_matches_key(branch, "TAT-123") is expected

    def test_group_by_module_with_exclude_and_unmapped(self):
        mods = context_repo.parse_module_map(MODULE_MAP)
        groups = reconciler.group_by_module([
            "project/apps/division_v2/views.py",
            "project/apps/division_v2/providers/cma_transfer/fsm.py",
            "project/apps/user_auth/views.py",
        ], mods, "BE")
        assert groups["CD"] == ["project/apps/division_v2/views.py"]
        assert groups["INV"] == ["project/apps/division_v2/providers/cma_transfer/fsm.py"]
        assert groups["_unmapped"] == ["project/apps/user_auth/views.py"]

    def test_env_files_cut_to_story_modules(self):
        mods = context_repo.parse_module_map(MODULE_MAP)
        files = [("M", "project/apps/division_v2/views.py"),
                 ("M", "project/apps/division_v2/providers/cma_transfer/fsm.py"),
                 ("A", "project/apps/user_auth/views.py")]
        kept, dropped = reconciler.env_files_in_scope(files, mods, "BE", {"CD"})
        assert kept == [("M", "project/apps/division_v2/views.py")] and dropped == 2
        assert reconciler.env_files_in_scope(files, mods, "BE", set()) == (files, 0)

    def test_coverage_precheck(self):
        cases = reconciler.tag_cases([
            {"key": "TAT-TC-1", "title": "[BR-CD-056] new rule works"},
            {"key": "TAT-TC-2", "title": "[BR-CD-001, BR-CD-999] regression"},
            {"key": "TAT-TC-3", "title": "untagged case"},
        ])
        res = reconciler.coverage_precheck({"BR-CD-001", "BR-CD-055", "BR-CD-056"}, cases)
        assert res["rules_without_case"] == ["BR-CD-055"]
        assert res["cases_without_rule"] == ["TAT-TC-3"]
        assert res["cases_with_unknown_rule"] == ["TAT-TC-2 → BR-CD-999"]

    def test_finds_branch_and_changed_files_against_base(self, tmp_path):
        origin = tmp_path / "origin"
        origin.mkdir()
        _run(origin, "init", "-q", "-b", "production")
        (origin / "a.py").write_text("x = 1\n")
        _commit_all(origin, "base")
        _run(origin, "checkout", "-q", "-b", "feature/TAT-7-change")
        (origin / "project").mkdir()
        (origin / "project/new.py").write_text("y = 2\n")
        (origin / "a.py").write_text("x = 3\n")
        _commit_all(origin, "TAT-7 change")
        _run(origin, "checkout", "-q", "production")
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)

        branches = reconciler.find_dev_branches(str(clone), "TAT-7")
        assert branches == ["origin/feature/TAT-7-change"]
        files = reconciler.changed_files(str(clone), "production", branches[0])
        assert sorted(files) == [("A", "project/new.py"), ("M", "a.py")]
        assert any("TAT-7 change" in c for c in reconciler.commit_log(str(clone), "production", branches[0]))

    @pytest.fixture
    def repo_with_pr(self, tmp_path):
        """origin: production + branch 'hotfix-no-key' + Azure-style refs/pull/5/merge."""
        origin = tmp_path / "origin"
        origin.mkdir()
        _run(origin, "init", "-q", "-b", "production")
        (origin / "a.py").write_text("x = 1\n")
        _commit_all(origin, "base")
        _run(origin, "checkout", "-q", "-b", "hotfix-no-key")
        (origin / "b.py").write_text("z = 1\n")
        _commit_all(origin, "fix without ticket key")
        _run(origin, "checkout", "-q", "production")
        _run(origin, "checkout", "-q", "-b", "tmp-merge")
        _run(origin, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-ff",
             "-m", "Merge pull request 5 from hotfix-no-key into production", "hotfix-no-key")
        _run(origin, "update-ref", "refs/pull/5/merge", "HEAD")
        _run(origin, "checkout", "-q", "production")
        _run(origin, "branch", "-q", "-D", "tmp-merge")
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
        return str(clone)

    @pytest.mark.parametrize("raw,expected", [
        ("a, b\nc", ["a", "b", "c"]),
        ("", []),
    ])
    def test_parse_attached_refs(self, raw, expected):
        assert reconciler.parse_attached_refs(raw) == expected

    def test_attach_branch_without_key(self, repo_with_pr):
        label, ref = reconciler.resolve_attached_ref(repo_with_pr, "hotfix-no-key")
        assert label == ref == "origin/hotfix-no-key"
        assert reconciler.changed_files(repo_with_pr, "production", ref) == [("A", "b.py")]

    @pytest.mark.parametrize("item", ["5", "#5", "https://dev.azure.com/o/p/_git/r/pullrequest/5"])
    def test_attach_pr_resolves_to_source_tip(self, repo_with_pr, item):
        label, ref = reconciler.resolve_attached_ref(repo_with_pr, item)
        assert label == "PR 5 (hotfix-no-key)"
        assert reconciler.changed_files(repo_with_pr, "production", ref) == [("A", "b.py")]
        # no named ref was added to the clone
        refs = _git_out(repo_with_pr, "for-each-ref", "--format=%(refname)")
        assert "pull" not in refs

    def test_attach_unknown_pr_or_branch_raises(self, repo_with_pr):
        with pytest.raises(reconciler.ReconcileError, match="PR 99 not found"):
            reconciler.resolve_attached_ref(repo_with_pr, "99")
        with pytest.raises(reconciler.ReconcileError, match="not found"):
            reconciler.resolve_attached_ref(repo_with_pr, "nope")


def _git_out(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


class TestStoryRulesForTestDesign:
    def test_requires_ticket_branch(self, ctx_repo):
        mods, block, err = context_repo.load_story_rules("TAT-9", "Fix IBAN screen")
        assert mods == [] and block == "" and "run 'Review userstory' first" in err

    def test_reads_rules_and_planned_from_ticket_branch(self, ctx_repo):
        context_repo.commit_file_to_branch(
            "TAT-9", "modules/cash-division/planned/TAT-9.md", "| BR-CD-056 | new | 🕓 |\n", "planned")
        mods, block, err = context_repo.load_story_rules("TAT-9", "unrelated text")
        assert [m["id"] for m in mods] == ["CD"] and err == ""
        assert "BR-CD-056" in block and "BR-CD-001" in block and "branch TAT-9" in block

    def test_branch_content_wins_over_master(self, ctx_repo):
        context_repo.commit_file_to_branch("TAT-9", "modules/cash-division/rules.md",
                                           "| BR-CD-099 | branch only | ☐ |\n", "edit")
        _, block, _ = context_repo.load_story_rules("TAT-9", "IBAN")
        assert "BR-CD-099" in block and "BR-CD-055" not in block

    def test_no_module_matched_is_an_error(self, ctx_repo):
        context_repo.commit_file_to_branch("TAT-9", "notes.md", "x", "empty")
        mods, _, err = context_repo.load_story_rules("TAT-9", "nothing relevant")
        assert mods == [] and "No module matched" in err


class TestTitleNormalisation:
    def test_rule_prefixes_ignored_when_matching_existing_cases(self):
        import qa_agent
        assert qa_agent._norm_title("[BR-TK-021][BR-TK-022]  Cancel  ticket") == "cancel ticket"
        assert qa_agent._norm_title("[NO-RULE] Cancel ticket") == qa_agent._norm_title("Cancel ticket")
