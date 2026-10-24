"""Offline tests for guard.py: one case per row of the table, no network."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import guard  # noqa: E402

FOUNDERS = ["@Inventeer/role-founder", "@Inventeer/role-leadership"]

BASE_MANIFEST = """---
id: sample
bot_level: L1
autonomy: Acts with approval
scope:
  does:
    - Read the queue
    - Draft a reply
permissions:
  - resource: hub
    kind: repo
    access: write
  - resource: tracker:INV
    kind: connector
    access: write
non_disclosure: >
  Nothing leaves the account.
approved_output_formats:
  - Go or no-go
  - Rating without figures or names
---

# Sample
"""


def manifest(**changes):
    """BASE_MANIFEST with one header edit applied as a text replacement."""
    text = BASE_MANIFEST
    for old, new in changes.items():
        old = old.replace("__", " ")
        text = text.replace(old, new)
    return text


def run(files, bot=False, base=None, head=None, codeowners=None):
    return guard.evaluate(
        files,
        author_is_bot=bot,
        founder_owners=FOUNDERS,
        read_base=lambda p: base,
        read_head=lambda p: head,
        base_codeowners=codeowners,
    )


def f(path, status="modified", previous=None):
    item = {"filename": path, "status": status}
    if previous:
        item["previous_filename"] = previous
    return item


def rows(findings):
    return {x.row for x in findings}


class PathRows(unittest.TestCase):
    def test_a_pull_request_touching_no_row_passes(self):
        self.assertEqual(run([f("src/app.py"), f("docs/guide.md"), f("assistants-notes/a.md")]), [])

    def test_workflows_and_codeowners(self):
        for path in (".github/workflows/ci.yml", ".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
            self.assertEqual(rows(run([f(path)])), {guard.ROW_WORKFLOWS}, path)

    def test_pre_approvals_wherever_it_sits(self):
        for path in ("governance/pre-approvals.md", "head-office/governance/pre-approvals.md"):
            self.assertIn(guard.ROW_PREAPPROVALS, rows(run([f(path)])), path)
        self.assertEqual(run([f("notes/pre-approvals.md")]), [])

    def test_own_manifest_only_for_a_bot_author(self):
        self.assertIn(guard.ROW_OWN_MANIFEST, rows(run([f("assistants/exec.md")], bot=True)))
        self.assertNotIn(guard.ROW_OWN_MANIFEST, rows(run([f("assistants/exec.md")], bot=False)))

    def test_creating_or_deleting_under_assistants(self):
        for status in ("added", "removed", "renamed"):
            self.assertIn(guard.ROW_ASSISTANTS_FILE, rows(run([f("assistants/skills/x.md", status)])), status)
        self.assertIn(
            guard.ROW_ASSISTANTS_FILE,
            rows(run([f("docs/x.md", "renamed", previous="assistants/old.md")])),
        )
        self.assertNotIn(guard.ROW_ASSISTANTS_FILE, rows(run([f("assistants/skills/x.md", "modified")])))

    def test_tier_s_folders(self):
        for path in ("finance/a.md", "people/b.md", "hiring/c.md", "contracts/d.md", "payments/e.md"):
            self.assertEqual(rows(run([f(path)])), {guard.ROW_TIER_S}, path)
        self.assertEqual(run([f("src/finance/a.py")]), [])

    def test_hub_rules(self):
        for path in ("AGENTS.md", "docs/CLAUDE.md", "rules/authoring.md", "x/HUB_Naming_Standard.md"):
            self.assertEqual(rows(run([f(path)])), {guard.ROW_HUB_RULES}, path)
        self.assertEqual(run([f("src/rules/x.py")]), [])

    def test_case_does_not_hide_a_row(self):
        self.assertIn(guard.ROW_TIER_S, rows(run([f("Finance/a.md")])))
        self.assertIn(guard.ROW_WORKFLOWS, rows(run([f(".GitHub/Workflows/ci.yml")])))

    def test_a_rename_out_of_a_reserved_path_is_still_blocked(self):
        self.assertIn(guard.ROW_TIER_S, rows(run([f("docs/a.md", "renamed", previous="finance/a.md")])))

    def test_too_many_files_fail_closed(self):
        many = [f(f"src/{i}.py") for i in range(guard.PR_FILES_CAP)]
        self.assertEqual(rows(run(many)), {guard.ROW_UNDECIDABLE})


class ManifestFields(unittest.TestCase):
    def change(self, new_text, bot=False):
        return run([f("assistants/sample.md")], bot=bot, base=BASE_MANIFEST, head=new_text)

    def test_an_unchanged_header_passes(self):
        self.assertEqual(self.change(BASE_MANIFEST + "\nMore body text.\n"), [])

    def test_bot_level_raised_blocks_and_lowered_passes(self):
        self.assertEqual(rows(self.change(manifest(**{"bot_level: L1": "bot_level: L0"}))), {guard.ROW_MANIFEST_FIELD})
        self.assertEqual(self.change(manifest(**{"bot_level: L1": "bot_level: L2"})), [])

    def test_autonomy_raised_blocks_and_lowered_passes(self):
        up = manifest(**{"autonomy: Acts__with__approval": "autonomy: Acts alone"})
        down = manifest(**{"autonomy: Acts__with__approval": "autonomy: Reads"})
        self.assertEqual(rows(self.change(up)), {guard.ROW_MANIFEST_FIELD})
        self.assertEqual(self.change(down), [])

    def test_permission_added_or_broadened_blocks(self):
        added = manifest(**{"non_disclosure:": "  - resource: finance\n    kind: repo\n    access: read\nnon_disclosure:"})
        broadened = manifest(**{"resource: hub\n    kind: repo\n    access: write": "resource: hub\n    kind: repo\n    access: write"})
        self.assertEqual(rows(self.change(added)), {guard.ROW_MANIFEST_FIELD})
        self.assertEqual(self.change(broadened), [])  # unchanged text
        base = BASE_MANIFEST.replace("access: write", "access: read", 1)
        raised = run([f("assistants/sample.md")], base=base, head=BASE_MANIFEST)
        self.assertEqual(rows(raised), {guard.ROW_MANIFEST_FIELD})

    def test_permission_narrowed_or_deleted_passes(self):
        narrowed = BASE_MANIFEST.replace("access: write", "access: read", 1)
        self.assertEqual(self.change(narrowed), [])
        deleted = BASE_MANIFEST.replace("  - resource: tracker:INV\n    kind: connector\n    access: write\n", "")
        self.assertEqual(self.change(deleted), [])
        limited = BASE_MANIFEST.replace(
            "resource: tracker:INV\n    kind: connector\n    access: write",
            "resource: tracker:INV\n    kind: connector\n    access: write\n    limit: context-gap",
        )
        self.assertEqual(self.change(limited), [])

    def test_a_limit_removed_or_changed_blocks(self):
        limited = BASE_MANIFEST.replace(
            "resource: tracker:INV\n    kind: connector\n    access: write",
            "resource: tracker:INV\n    kind: connector\n    access: write\n    limit: context-gap",
        )
        self.assertEqual(rows(run([f("assistants/sample.md")], base=limited, head=BASE_MANIFEST)), {guard.ROW_MANIFEST_FIELD})

    def test_scope_does_added_or_changed_blocks_and_deleted_passes(self):
        added = manifest(**{"- Draft a reply": "- Draft a reply\n    - Send the reply"})
        changed = manifest(**{"- Draft a reply": "- Send a reply"})
        deleted = manifest(**{"    - Draft a reply\n": ""})
        self.assertEqual(rows(self.change(added)), {guard.ROW_MANIFEST_FIELD})
        self.assertEqual(rows(self.change(changed)), {guard.ROW_MANIFEST_FIELD})
        self.assertEqual(self.change(deleted), [])

    def test_non_disclosure_changed_blocks(self):
        text = manifest(**{"Nothing leaves the account.": "Some things leave the account."})
        self.assertEqual(rows(self.change(text)), {guard.ROW_MANIFEST_FIELD})
        reflowed = manifest(**{"Nothing leaves the account.": "Nothing   leaves\n  the account."})
        self.assertEqual(self.change(reflowed), [])

    def test_approved_output_formats_added_blocks_and_removed_passes(self):
        added = manifest(**{"- Go or no-go": "- Go or no-go\n  - Full figures"})
        removed = manifest(**{"  - Go or no-go\n": ""})
        self.assertEqual(rows(self.change(added)), {guard.ROW_MANIFEST_FIELD})
        self.assertEqual(self.change(removed), [])

    def test_other_header_and_body_edits_pass(self):
        text = manifest(**{"id: sample": "id: sample\ndescription: A new description"})
        self.assertEqual(self.change(text), [])

    def test_a_header_that_cannot_be_read_fails_closed(self):
        broken = BASE_MANIFEST.replace("bot_level: L1", "bot_level: [L1")
        self.assertEqual(rows(self.change(broken)), {guard.ROW_UNDECIDABLE})

    def test_a_file_without_a_header_is_not_a_manifest(self):
        self.assertEqual(run([f("assistants/README.md")], base="# Notes\n", head="# Notes\nmore\n"), [])

    def test_a_header_added_or_removed_blocks(self):
        self.assertEqual(
            rows(run([f("assistants/x.md")], base="# plain\n", head=BASE_MANIFEST)),
            {guard.ROW_MANIFEST_FIELD},
        )


class CodeOwners(unittest.TestCase):
    OWNERS = (
        "# comment\n"
        "* @Inventeer/area-ops\n"
        "/.github/ @Inventeer/role-leadership\n"
        "/strategy/ @Inventeer/role-leadership # founders\n"
        "/strategy/notes/ @Inventeer/area-ops\n"
        "*.tf @Inventeer/role-founder @Inventeer/area-infra\n"
    )

    def test_a_path_the_founder_owns_blocks(self):
        for path in ("strategy/plan.md", "infra/main.tf"):
            found = run([f(path)], codeowners=self.OWNERS)
            self.assertEqual(rows(found), {guard.ROW_CODEOWNERS_FOUNDER}, path)

    def test_the_last_matching_rule_wins(self):
        self.assertEqual(run([f("strategy/notes/a.md")], codeowners=self.OWNERS), [])

    def test_a_path_with_other_owners_passes(self):
        self.assertEqual(run([f("src/app.py")], codeowners=self.OWNERS), [])

    def test_owner_names_are_case_insensitive(self):
        found = run([f("strategy/plan.md")], codeowners="/strategy/ @INVENTEER/Role-Leadership\n")
        self.assertEqual(rows(found), {guard.ROW_CODEOWNERS_FOUNDER})

    def test_no_codeowners_file_adds_no_row(self):
        self.assertEqual(run([f("strategy/plan.md")], codeowners=None), [])

    def test_a_renamed_path_is_checked_at_its_old_place(self):
        found = run([f("docs/plan.md", "renamed", previous="strategy/plan.md")], codeowners=self.OWNERS)
        self.assertEqual(rows(found), {guard.ROW_CODEOWNERS_FOUNDER})


class Reporting(unittest.TestCase):
    def test_the_alert_names_repository_pull_request_and_path(self):
        findings = [guard.Finding(guard.ROW_TIER_S, "finance/<b>&.md")]
        text = guard.alert_text("Inventeer/sandbox-demo", "12", "clara[bot]", findings)
        self.assertIn("Inventeer/sandbox-demo", text)
        self.assertIn("https://github.com/Inventeer/sandbox-demo/pull/12", text)
        self.assertIn("finance/&lt;b&gt;&amp;.md", text)
        self.assertNotIn("<b>", text)

    def test_the_alert_is_capped(self):
        findings = [guard.Finding(guard.ROW_TIER_S, f"finance/{i}.md") for i in range(40)]
        text = guard.alert_text("Inventeer/x", "1", "a", findings)
        self.assertIn("and 28 more", text)

    def test_the_digest_ignores_order_and_changes_with_the_findings(self):
        a = guard.Finding(guard.ROW_TIER_S, "finance/a.md")
        b = guard.Finding(guard.ROW_TIER_S, "people/b.md")
        self.assertEqual(guard.digest([a, b]), guard.digest([b, a]))
        self.assertNotEqual(guard.digest([a]), guard.digest([a, b]))


if __name__ == "__main__":
    unittest.main()
