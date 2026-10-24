# tool-confidentiality-gate

The entry point to Inventeer's confidentiality gate for the public repositories of the `Inventeer` organization.

GitHub shares a private repository's reusable workflow with private repositories only, so a public repository cannot call the gate where it lives. This repository holds a byte-for-byte copy of the gate's bootstrap, `.github/workflows/confidentiality-gate.yml`. The bootstrap holds no logic. It fetches the gate's scripts from Inventeer's private Hub with a credential that only granted repositories of `Inventeer` hold, then runs them against the calling repository: the confidentiality lint and the code-owner check.

## Calling it

A public repository of `Inventeer`, once the organization's owners have granted it the Hub reader credential, adds `.github/workflows/confidentiality-gate.yml`:

```yaml
name: Confidentiality Gate

on:
  pull_request:
    branches: [main]

concurrency:
  group: confidentiality-gate-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  gate:
    uses: Inventeer/tool-confidentiality-gate/.github/workflows/confidentiality-gate.yml@main
    with:
      hub_reader_app_id: ${{ vars.HUB_READER_APP_ID }}
      allow_sensitivity: "O"
      max_tier: open
      company_tenant_direction: none
    secrets:
      HUB_READER_APP_PRIVATE_KEY: ${{ secrets.HUB_READER_APP_PRIVATE_KEY }}
```

The required status check is named `gate / confidentiality-gate`.

A public repository holds only the base tier, so the gate requires `max_tier: open` and `allow_sensitivity: "O"` from any repository that is not private, and fails a pull request that adds a folder classified above `open`.

## What it refuses

The following runs end red before the job reads any credential:

- a pull request opened from a fork; to have it gated, a maintainer pushes its branch to the repository itself;
- any run started by `pull_request_target`;
- a repository whose owner is not `Inventeer` or `Inventeer-Management`.

A public run's log shows the verdict and the calling repository's own paths, nothing of the Hub.

## Its own pull requests

This repository is gated like any public caller. `.github/workflows/self-gate.yml` calls the copy here at `main` with `max_tier: open` and `allow_sensitivity: "O"`, so a pull request that changes the copy is checked by the copy already in force. For that call the repository holds the Hub reader credential, granted by name like any other public caller's. `gate / confidentiality-gate` is a required check on `main`.

## Changing it

Do not edit the bootstrap here first. It changes in the Hub, and this copy follows in a pull request of its own. A check in the Hub fails while the two differ. The reserved-path guard below is the exception: it is defined here, once.

## The reserved-path guard

`.github/workflows/reserved-guard.yml` is the one definition of the `reserved-guard` check of `HUB_Bot_Work_Protocol_Standard` (section 2.12), for every repository of the `Inventeer` organization that a bot's GitHub App is installed on. It lives here because this repository is public, and a public repository's workflow is the only kind both public and private repositories can call.

The check decides only for the pull requests of the bots it guards: the logins in the `guarded_authors` input, today the Executive Assistant's App, `clara-foster-inventeer[bot]`. A bot joins the guard by one pull request that adds its login to that default. A pull request of any other author, a person included, passes at once, because a person's change is governed by the code owners' review and a required check that failed it would stop the team's own work.

For a guarded author, the check fails the pull request when it touches a path the founder reserves:

- `.github/workflows/**` and every `CODEOWNERS` file;
- `governance/pre-approvals.md`, wherever it sits;
- the opening bot's own manifest, `assistants/<id>.md` (a guarded bot is blocked on any manifest directly under `assistants/`);
- the creation or deletion of any file under `assistants/`;
- in a manifest under `assistants/`: a change to `bot_level`, `permissions`, `autonomy`, `non_disclosure` or `approved_output_formats`, or an added or changed `scope.does` line (a change that only removes access passes);
- `finance/**`, `people/**`, `hiring/**`, `contracts/**` and `payments/**` at the repository root;
- `AGENTS.md`, `CLAUDE.md`, `rules/**` and every `HUB_` artifact;
- any path a `CODEOWNERS` rule of the base branch assigns to the founder.

A blocked pull request gets the `founder-review` label, one comment and one Slack alert naming the repository, the pull request and the path or field. The check merges nothing and approves nothing. It reads the pull request through the API and never checks out or runs its code. When it cannot decide, it fails.

### Calling it

```yaml
name: Reserved Guard

on:
  pull_request:
    types: [opened, synchronize, reopened, edited]

concurrency:
  group: reserved-guard-${{ github.event.pull_request.number }}
  cancel-in-progress: true

permissions:
  contents: read
  pull-requests: write

jobs:
  guard:
    uses: Inventeer/tool-confidentiality-gate/.github/workflows/reserved-guard.yml@main
    secrets:
      SLACK_WEBHOOK_FOUNDER_REVIEW: ${{ secrets.SLACK_WEBHOOK_FOUNDER_REVIEW }}
```

The required status check is named `guard / reserved-guard`, registered in the organization's `Reserved guard` ruleset for the default branch. The ruleset has no bypass actor.

The guard's code and its offline tests are in `.github/reserved-guard/`; `.github/workflows/reserved-guard-tests.yml` runs them on a pull request that changes the guard.
