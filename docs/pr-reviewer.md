# Automatic Codex PR review

## Enable in GitHub

With the workflow on the trusted base branch, open the repository’s **Settings → Secrets and variables → Actions**. Under **Variables**, set `CODEX_PR_REVIEW_ENABLED` to `true`. Under **Secrets**, add `OPENAI_API_KEY` with access to a model supported by Codex CLI. Opening, reopening, synchronizing, or marking a PR ready for review can trigger a run. Drafts are skipped, and newer runs cancel older runs for the same PR. Change or remove the variable to disable future runs.

For public repositories, GitHub may require an [Actions event policy](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target#default-policy-for-pull_request_target) that allows `pull_request_target`. Scope any exception to `.github/workflows/codex-pr-review.yml`.

Already-reviewed head/base pairs and outdated PR events skip before Codex starts. The triggering head must still match, and capture uses the current base from GitHub. Both commits are checked again before publishing.

The workflow uses the built-in GitHub Actions token. Generation has repository contents and PR read access. Only its review step receives the OpenAI key, and the Codex process receives an isolated environment without the GitHub token. The publisher has repository contents read and PR write access. It uses the generated JSON with the trusted Python script. Both jobs check out the workflow’s commit with persisted checkout credentials disabled. Fork and same-repository PR contents enter the workflow through GitHub’s changed-files API as data.

Patches selected for review are sent to OpenAI. The Actions artifact contains only the bounded snapshot and review result and is retained for three days. Repository users with access to the workflow run can download it, so consider that access when enabling the workflow.

## Run locally

Use Python 3.11+, an authenticated GitHub CLI, and Codex CLI. The workflow installs Codex CLI `0.171.0`. Authenticate `gh` with `gh auth login` or provide a `GH_TOKEN` with PR read access for the target repository. Set `OPENAI_API_KEY` in your local environment before running `review`.

```sh
npm install -g @openai/codex@0.171.0
repo=owner/repository
pr=123
review_dir="$(mktemp -d)"
python3 pr_reviewer.py capture --repo "$repo" --pr "$pr" \
  --snapshot "$review_dir/snapshot.json"
python3 pr_reviewer.py review --snapshot "$review_dir/snapshot.json" \
  --result "$review_dir/result.json"
python3 pr_reviewer.py publish --repo "$repo" --pr "$pr" \
  --snapshot "$review_dir/snapshot.json" --result "$review_dir/result.json" --dry-run
```

The dry run performs live PR reads and prints the intended JSON if the PR is still open and non-draft, the snapshot still matches, and no GitHub Actions bot review exists for that head and base. Without `--dry-run`, the command requires PR write access and submits a `COMMENT` review for the captured head SHA. Duplicate suppression recognizes reviews authored by `github-actions[bot]`, so it does not recognize reviews posted locally under a personal account.

## Coverage and limits

- Capture reads up to GitHub’s 3,000-file API limit and checks PR metadata again after fetching patches. Binary files, missing patches, incomplete hunks, and patches that exceed the capture limits are skipped. Capture limits each serialized patch to 64 KiB and retained patches to 768 KiB total. The snapshot and result each have a 16 MiB file limit.
- Review defaults to six sequential batches with a 32 KiB prompt budget per batch. For local runs, `--max-batches` accepts 1–12 and `--max-prompt-bytes` accepts 1–65,536. Each eligible patch must fit in a single batch. Files without added lines are skipped because inline findings require the exact path and an added line in an included patch.
- The published review summarizes coverage and skip reasons, including changed files GitHub omitted. It can include up to 20 inline findings, or a summary alone when there are no qualifying findings. Codex or validation failures fail generation and prevent publication.
- The publisher checks the snapshot, current PR status, head, base, and review history before calling GitHub. GitHub’s review endpoint does not make the final metadata read and submission atomic, so there is a short race window before posting.

## Framework choice

GitHub Actions runs Codex and separates read access from review publication. The native pull-request review API supports a single comment review tied to a commit, with inline comments on validated lines. [Reviewdog](https://github.com/reviewdog/reviewdog) primarily reports linter diagnostics. [Danger](https://danger.systems/js/) primarily runs scripted PR rules. The [Codex GitHub Action](https://github.com/openai/codex-action) can run a single Codex invocation. This workflow calls Codex CLI directly so the same batching and line validation run locally and in CI. The reviewer’s Python code handles model output, exact line validation, stale snapshots, and duplicate reviews directly.
