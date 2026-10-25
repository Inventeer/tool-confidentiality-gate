#!/usr/bin/env python3
"""reserved-guard: block a pull request that touches a path the founder reserves.

One definition for every repository (HUB_Bot_Work_Protocol_Standard, section
2.12). The reusable workflow .github/workflows/reserved-guard.yml runs this
script on a pull request. The script reads the pull request through the API
only; it never checks out or runs the pull request's code.

What it blocks (the table of section 2.12, rule 2):
  1. .github/workflows/** and every CODEOWNERS file
  2. governance/pre-approvals.md, wherever it sits
  3. the opening bot's own manifest, assistants/<id>.md
  4. the creation or deletion of any file under assistants/
  5. in a manifest under assistants/: a change to bot_level, permissions,
     autonomy, non_disclosure or approved_output_formats, or an added or
     changed scope.does line (a part that only removes access passes)
  6. finance/**, people/**, hiring/**, contracts/** and payments/**
  7. AGENTS.md, CLAUDE.md, rules/** and every HUB_ artifact
  8. any path a CODEOWNERS rule assigns to the founder, except by the
     catch-all rule *: it names no path, and the ruleset's code-owner review
     already requires the founder's approval everywhere it applies

A blocked pull request gets the founder-review label, one comment and one Slack
alert, and the check fails. The guard merges nothing and approves nothing.

The guard decides only for the pull requests of the authors it guards: the bots
listed in GUARDED_AUTHORS, whose merge it gates (section 2.7, rule 3). A pull
request of any other author, a person included, passes at once: a person's
change is governed by the code owners' review, and a required check that failed
it would stop the team's own work.

Inputs (environment): GH_TOKEN, REPO, PR_NUMBER, BASE_REF, HEAD_SHA, PR_AUTHOR,
GUARDED_AUTHORS (space separated logins), FOUNDER_OWNERS (space separated),
SLACK_WEBHOOK_URL (optional).
Exit code: 0 pass, 1 blocked, 2 the guard could not decide (fails closed).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import pathspec
import yaml

LABEL = "founder-review"
LABEL_COLOR = "B60205"
MARKER_AUTHOR = "github-actions[bot]"
PR_FILES_CAP = 3000
SLACK_LINES = 12

HUB_ROOT_DIRS = ("finance", "people", "hiring", "contracts", "payments")
CODEOWNERS_CATCH_ALL = "*"

ROW_WORKFLOWS = "workflows and CODEOWNERS files"
ROW_PREAPPROVALS = "the founder's pre-approvals file"
ROW_OWN_MANIFEST = "the opening bot's own manifest"
ROW_ASSISTANTS_FILE = "creation or deletion of a file under assistants/"
ROW_MANIFEST_FIELD = "a reserved field of a manifest"
ROW_TIER_S = "finance, people, hiring, contracts and payments data"
ROW_HUB_RULES = "Hub rules (AGENTS.md, CLAUDE.md, rules/, HUB_ artifacts)"
ROW_CODEOWNERS_FOUNDER = "a path a CODEOWNERS rule assigns to the founder"
ROW_UNDECIDABLE = "a change the guard cannot read"

AUTONOMY_ORDER = ("reads", "drafts", "acts with approval", "acts alone")
ACCESS_ORDER = {"read": 0, "write": 1}


class GuardError(Exception):
    """The guard could not decide; the check fails closed."""


@dataclass(frozen=True)
class Finding:
    row: str
    path: str
    detail: str = ""

    def line(self) -> str:
        tail = f" ({self.detail})" if self.detail else ""
        return f"{self.row}: {self.path}{tail}"


# --------------------------------------------------------------------------
# Manifest comparison
# --------------------------------------------------------------------------


def front_matter(text: str | None):
    """The YAML header of a manifest as a dict, None when there is none."""
    if text is None:
        return None
    m = re.match(r"---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not m:
        return None
    data = yaml.safe_load(m.group(1))
    return data if isinstance(data, dict) else None


def _level(value):
    m = re.fullmatch(r"L(\d+)", str(value).strip())
    return int(m.group(1)) if m else None


def _norm(value) -> str:
    return " ".join(str(value).split())


def _lines(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        return [_norm(value)]
    return [_norm(v) for v in value]


def _permission_no_broader(head: dict, base: dict) -> bool:
    if not isinstance(head, dict) or not isinstance(base, dict):
        return head == base
    for key in set(head) | set(base):
        if key in ("access", "limit"):
            continue
        if head.get(key) != base.get(key):
            return False
    h_acc, b_acc = head.get("access"), base.get("access")
    if h_acc != b_acc:
        h_rank = ACCESS_ORDER.get(str(h_acc).strip().lower())
        b_rank = ACCESS_ORDER.get(str(b_acc).strip().lower())
        if h_rank is None or b_rank is None or h_rank > b_rank:
            return False
    if base.get("limit") is not None and head.get("limit") != base.get("limit"):
        return False
    return True


def manifest_changes(base: dict, head: dict) -> list[str]:
    """The reserved parts a manifest change gives or changes.

    A part that only removes access is not reserved: a lower bot_level or
    autonomy, a permission deleted or narrowed, an approved output format
    deleted, a scope.does line deleted.
    """
    found: list[str] = []

    if head.get("bot_level") != base.get("bot_level"):
        h, b = _level(head.get("bot_level")), _level(base.get("bot_level"))
        # L0 is the highest level, so a larger number gives up access.
        if h is None or b is None or h < b:
            found.append("bot_level")

    if head.get("autonomy") != base.get("autonomy"):
        h = _norm(head.get("autonomy")).lower()
        b = _norm(base.get("autonomy")).lower()
        if (
            h not in AUTONOMY_ORDER
            or b not in AUTONOMY_ORDER
            or AUTONOMY_ORDER.index(h) > AUTONOMY_ORDER.index(b)
        ):
            found.append("autonomy")

    if _norm(head.get("non_disclosure", "")) != _norm(base.get("non_disclosure", "")):
        found.append("non_disclosure")

    if not set(_lines(head.get("approved_output_formats"))) <= set(
        _lines(base.get("approved_output_formats"))
    ):
        found.append("approved_output_formats")

    base_perms = base.get("permissions") or []
    for entry in head.get("permissions") or []:
        if not any(_permission_no_broader(entry, b) for b in base_perms):
            res = entry.get("resource") if isinstance(entry, dict) else entry
            found.append(f"permissions ({res})")

    head_scope = head.get("scope") if isinstance(head.get("scope"), dict) else {}
    base_scope = base.get("scope") if isinstance(base.get("scope"), dict) else {}
    added = [
        line
        for line in _lines(head_scope.get("does"))
        if line not in set(_lines(base_scope.get("does")))
    ]
    if added:
        found.append("scope.does (a line added or changed)")
    return found


# --------------------------------------------------------------------------
# CODEOWNERS
# --------------------------------------------------------------------------


def parse_codeowners(text: str) -> list[tuple[str, list[str]]]:
    rules = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tokens = []
        for token in line.split():
            if token.startswith("#"):
                break
            tokens.append(token)
        if not tokens:
            continue
        rules.append((tokens[0], [t.lower() for t in tokens[1:]]))
    return rules


def codeowners_rule_for(path: str, rules, _cache={}):
    """The last CODEOWNERS rule that matches the path, as (pattern, owners)."""
    hit = None
    for pattern, owners in rules:
        spec = _cache.get(pattern)
        if spec is None:
            spec = pathspec.PathSpec.from_lines("gitwildmatch", [pattern])
            _cache[pattern] = spec
        if spec.match_file(path):
            hit = (pattern, owners)
    return hit


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------


def _paths(f: dict) -> list[str]:
    out = [f["filename"]]
    if f.get("previous_filename"):
        out.append(f["previous_filename"])
    return out


def is_guarded(author: str, guarded: list[str]) -> bool:
    """True when the pull request's author is one of the bots the guard gates."""
    return author.strip().lower() in {g.strip().lower() for g in guarded if g.strip()}


def path_findings(f: dict, author_is_bot: bool) -> list[Finding]:
    found: list[Finding] = []
    status = f.get("status", "")
    for path in _paths(f):
        low = path.lower()
        parts = low.split("/")
        name = parts[-1]
        if low.startswith(".github/workflows/") or name == "codeowners":
            found.append(Finding(ROW_WORKFLOWS, path))
        if re.search(r"(^|/)governance/pre-approvals\.md$", low):
            found.append(Finding(ROW_PREAPPROVALS, path))
        if parts[0] in HUB_ROOT_DIRS and len(parts) > 1:
            found.append(Finding(ROW_TIER_S, path))
        if (
            name in ("agents.md", "claude.md")
            or (parts[0] == "rules" and len(parts) > 1)
            or name.startswith("hub_")
        ):
            found.append(Finding(ROW_HUB_RULES, path))
        if parts[0] == "assistants" and len(parts) > 1:
            if status in ("added", "removed", "renamed", "copied"):
                found.append(Finding(ROW_ASSISTANTS_FILE, path, status))
            if author_is_bot and len(parts) == 2 and name.endswith(".md"):
                found.append(Finding(ROW_OWN_MANIFEST, path))
    return found


def evaluate(
    files: list[dict],
    author_is_bot: bool,
    founder_owners: list[str],
    read_base,
    read_head,
    base_codeowners: str | None,
) -> list[Finding]:
    """Every reserved row the pull request touches.

    read_base(path) and read_head(path) return a file's text at the merge base
    and at the head, None when the file does not exist there.
    """
    found: list[Finding] = []
    if len(files) >= PR_FILES_CAP:
        found.append(
            Finding(ROW_UNDECIDABLE, "pull request", f"{len(files)} files or more")
        )

    for f in files:
        found.extend(path_findings(f, author_is_bot))

        path = f["filename"]
        parts = path.split("/")
        if (
            f.get("status") in ("modified", "changed")
            and len(parts) == 2
            and parts[0].lower() == "assistants"
            and parts[1].lower().endswith(".md")
        ):
            try:
                base, head = front_matter(read_base(path)), front_matter(read_head(path))
            except yaml.YAMLError as err:
                found.append(Finding(ROW_UNDECIDABLE, path, f"header not readable: {err.__class__.__name__}"))
                continue
            if base is not None or head is not None:
                if base is None or head is None:
                    found.append(Finding(ROW_MANIFEST_FIELD, path, "manifest header added or removed"))
                else:
                    for detail in manifest_changes(base, head):
                        found.append(Finding(ROW_MANIFEST_FIELD, path, detail))

    if base_codeowners is not None:
        rules = parse_codeowners(base_codeowners)
        founders = {o.lower() for o in founder_owners}
        for f in files:
            for path in _paths(f):
                hit = codeowners_rule_for(path, rules)
                # The catch-all names no path, so it reserves none (section 2.12).
                if hit and hit[0] != CODEOWNERS_CATCH_ALL and founders.intersection(hit[1]):
                    found.append(Finding(ROW_CODEOWNERS_FOUNDER, path, f"rule {hit[0]}"))

    unique = {}
    for item in found:
        unique[(item.row, item.path, item.detail)] = item
    return list(unique.values())


# --------------------------------------------------------------------------
# GitHub and Slack
# --------------------------------------------------------------------------


class GitHub:
    def __init__(self, repo: str, token: str):
        self.repo, self.token = repo, token

    def _request(self, method, url, body=None, accept="application/vnd.github+json"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", accept)
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

    def api(self, method, path, body=None):
        raw = self._request(method, f"https://api.github.com/repos/{self.repo}{path}", body)
        return json.loads(raw) if raw else None

    def paged(self, path):
        out, page = [], 1
        while True:
            sep = "&" if "?" in path else "?"
            chunk = self.api("GET", f"{path}{sep}per_page=100&page={page}")
            out.extend(chunk)
            if len(chunk) < 100:
                return out
            page += 1

    def text(self, path: str, ref: str):
        url = (
            f"https://api.github.com/repos/{self.repo}/contents/"
            f"{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}"
        )
        try:
            return self._request("GET", url, accept="application/vnd.github.raw+json").decode()
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None
            raise


def slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pr_url(repo: str, number: str) -> str:
    return f"https://github.com/{repo}/pull/{number}"


def alert_text(repo: str, number: str, author: str) -> str:
    """The plain-text fallback: what a notification and a client without cards show."""
    return f"Founder review needed: {repo}#{number} by {author}"


def alert_payload(repo: str, number: str, author: str, findings: list[Finding]) -> dict:
    """The Slack card: a title, three fields, the reasons grouped by row and a button."""
    url = _pr_url(repo, number)
    groups: dict[str, list[Finding]] = {}
    for item in findings:
        groups.setdefault(item.row, []).append(item)

    lines: list[str] = []
    shown = 0
    for row, items in groups.items():
        if shown >= SLACK_LINES:
            break
        lines.append(f"*{slack_escape(row[:1].upper() + row[1:])}*")
        for item in items:
            if shown >= SLACK_LINES:
                break
            path = slack_escape(item.path).replace("`", "'")
            detail = f" _({slack_escape(item.detail)})_" if item.detail else ""
            lines.append(f"\u2022 `{path}`{detail}")
            shown += 1
    if len(findings) > shown:
        lines.append(f"_and {len(findings) - shown} more_")

    return {
        "text": alert_text(repo, number, author),
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "Founder review needed"}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Repository*\n{slack_escape(repo)}"},
                    {"type": "mrkdwn", "text": f"*Pull request*\n<{url}|#{number}>"},
                    {"type": "mrkdwn", "text": f"*Opened by*\n{slack_escape(author)}"},
                ],
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Review pull request"},
                        "url": url,
                        "style": "primary",
                    }
                ],
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "reserved-guard blocked this pull request. It touches a path the founder reserves.",
                    }
                ],
            },
        ],
    }


def post_slack(webhook: str, payload: dict) -> None:
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


def digest(findings: list[Finding]) -> str:
    body = "\n".join(sorted(f"{f.row}|{f.path}|{f.detail}" for f in findings))
    return hashlib.sha256(body.encode()).hexdigest()[:12]


def comment_body(findings: list[Finding], marker: str) -> str:
    lines = [f"- {f.line()}" for f in findings[:50]]
    return (
        f"<!-- {marker} -->\n"
        "**reserved-guard**: this pull request touches what "
        "`HUB_Bot_Work_Protocol_Standard` section 2.12 reserves for the founder, "
        "so the `reserved-guard` check fails and the founder reviews it.\n\n"
        + "\n".join(lines)
    )


def act_on_block(gh: GitHub, number: str, author: str, findings, webhook: str) -> None:
    marker = f"reserved-guard:{digest(findings)}"
    try:
        try:
            gh.api("POST", "/labels", {"name": LABEL, "color": LABEL_COLOR,
                                       "description": "Reserved by reserved-guard: the founder reviews it"})
        except urllib.error.HTTPError as err:
            if err.code != 422:
                raise
        gh.api("POST", f"/issues/{number}/labels", {"labels": [LABEL]})
        seen = any(
            marker in (c.get("body") or "") and c["user"]["login"] == MARKER_AUTHOR
            for c in gh.paged(f"/issues/{number}/comments")
        )
        if seen:
            return
        gh.api("POST", f"/issues/{number}/comments", {"body": comment_body(findings, marker)})
        if webhook:
            post_slack(webhook, alert_payload(gh.repo, number, author, findings))
        else:
            print("::warning::reserved-guard: no Slack webhook secret, no alert sent")
    except (urllib.error.URLError, OSError) as err:
        # The check is already red; a label or alert that cannot be sent never turns it green.
        print(f"::warning::reserved-guard: label or alert not sent ({err.__class__.__name__})")


def clear_label_if_ours(gh: GitHub, number: str) -> None:
    try:
        labels = [l["name"] for l in gh.paged(f"/issues/{number}/labels")]
        if LABEL not in labels:
            return
        ours = any(
            "reserved-guard:" in (c.get("body") or "") and c["user"]["login"] == MARKER_AUTHOR
            for c in gh.paged(f"/issues/{number}/comments")
        )
        if ours:
            gh.api("DELETE", f"/issues/{number}/labels/{urllib.parse.quote(LABEL)}")
    except (urllib.error.URLError, OSError) as err:
        print(f"::warning::reserved-guard: label not cleared ({err.__class__.__name__})")


def main(env=os.environ) -> int:
    try:
        author = env.get("PR_AUTHOR", "")
        if not is_guarded(author, env.get("GUARDED_AUTHORS", "").split()):
            print(f"reserved-guard: passed, {author or 'this author'} is not a guarded bot")
            return 0
        repo, number = env["REPO"], env["PR_NUMBER"]
        if not number:
            raise GuardError("the guard runs on a pull request only")
        gh = GitHub(repo, env["GH_TOKEN"])
        head_sha, base_ref = env["HEAD_SHA"], env["BASE_REF"]
        author_is_bot = True  # a guarded author is a bot, so its own manifest is reserved
        founders = env.get("FOUNDER_OWNERS", "").split()

        files = gh.paged(f"/pulls/{number}/files")
        base_tip = gh.api("GET", f"/commits/{urllib.parse.quote(base_ref)}")["sha"]
        merge_base = gh.api(
            "GET", f"/compare/{base_tip}...{head_sha}?per_page=1"
        )["merge_base_commit"]["sha"]

        codeowners = None
        for where in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
            codeowners = gh.text(where, base_tip)
            if codeowners is not None:
                break

        findings = evaluate(
            files,
            author_is_bot,
            founders,
            lambda p: gh.text(p, merge_base),
            lambda p: gh.text(p, head_sha),
            codeowners,
        )
    except (GuardError, KeyError, urllib.error.URLError, OSError, ValueError) as err:
        print(f"::error::reserved-guard could not decide: {err.__class__.__name__}: {err}")
        return 2

    if findings:
        print("reserved-guard: blocked")
        for f in findings:
            print(f"  - {f.line()}")
        act_on_block(gh, number, author, findings, env.get("SLACK_WEBHOOK_URL", ""))
        return 1

    print("reserved-guard: passed, the pull request touches no reserved row")
    clear_label_if_ours(gh, number)
    return 0


if __name__ == "__main__":
    sys.exit(main())
