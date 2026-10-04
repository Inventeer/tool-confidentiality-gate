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

Do not edit the workflow here first. It changes in the Hub, and this copy follows in a pull request of its own. A check in the Hub fails while the two differ.
