# Automatic Inkling PR review

The worker uses the local `codex-inkling` launcher. Draft PRs are skipped. Eligible PRs are reviewed serially with one local model process at a time. GitHub Actions only validates and posts finished reviews. It receives finding text and line anchors through a repository dispatch, then refetches the PR data to verify the captured head, base, and diff before posting as `github-actions[bot]`.

## Set up the worker

1. Place `.github/workflows/codex-pr-review.yml` and `pr_reviewer.py` on the repository's default branch.
2. Install Python 3.11+, GitHub CLI, and a working `codex-inkling` launcher on the machine that will run Inkling. The launcher comes from your local Inkling/Codex setup.
3. Sign in to GitHub CLI with access to read PRs and dispatch repository events. For a fine-grained token, grant pull requests read and repository contents write on the target repo.
4. Schedule the following command every few minutes using your local scheduler, such as a macOS LaunchAgent. Use one worker per local Inkling instance to avoid concurrent generation. Give the scheduler a PATH that includes `gh` and `codex-inkling`.

```sh
python3.12 /path/to/codex-task-harness/pr_reviewer.py poll \
  --repo owner/repository --state-dir "$HOME/.local/state/inkling-pr-review"
```

The machine must be logged in, awake, and online for reviews to start. Add `--dry-run` to print the intended review payloads without dispatching. GitHub Actions must allow the `repository_dispatch` event for this workflow if you use a custom event policy.

No OpenAI API credentials are required. Fork patches enter Inkling as text. The local Codex process gets neither GitHub nor cloud-model credentials. GitHub Actions checks out the trusted workflow revision with persisted credentials disabled and never checks out or runs PR code.

## Review behavior

- The local worker reads open PRs, captures stable head/base snapshots, and skips commits already reviewed by the bot. Successful dispatches are recorded for 15 minutes to allow time for publication and retry intermittent failures. A short-lived lock prevents overlapping polls using the same state directory.
- Each review uses up to six batches of 12 KiB by default. The local worker divides these into requests of roughly 2.4 KB, down to hunks and lines. It splits again on output limits or invalid results, without changing the source line anchors or GitHub relay format. Scheduled retries resume from validated slices in the local state directory. An exhausted review reports failure instead of publishing incomplete coverage. Binary files, unavailable or truncated patches, oversized files, and deletion-only files can be skipped. The review body reports coverage and skip reasons.
- The GitHub workflow checks that the current snapshot matches the dispatch and publishes at most 20 validated added-line comments as a `COMMENT` review. No model runs in the publisher. A stale or already reviewed snapshot is skipped. A model or validation failure cannot publish a clean-review placeholder.
- GitHub's metadata read and review submission are separate calls. A PR can still move in between. Review comments are pinned to the captured head.

## Run a single dry review

```sh
repo=owner/repository
pr=123
review_dir="$(mktemp -d)"
python3.12 pr_reviewer.py capture --repo "$repo" --pr "$pr" \
  --snapshot "$review_dir/snapshot.json"
python3.12 pr_reviewer.py review --snapshot "$review_dir/snapshot.json" \
  --result "$review_dir/result.json"
python3.12 pr_reviewer.py publish --repo "$repo" --pr "$pr" \
  --snapshot "$review_dir/snapshot.json" --result "$review_dir/result.json" --dry-run
```

A direct `publish` without `--dry-run` posts as the active GitHub CLI identity. The scheduled `poll` command dispatches to the workflow so comments come from the bot. Duplicate suppression recognizes the bot's reviews.

## Framework choice

[Reviewdog](https://github.com/reviewdog/reviewdog) reports linter diagnostics, and [Danger](https://danger.systems/js/) runs scripted PR rules. The Inkling worker uses the native [GitHub review API](https://docs.github.com/en/rest/pulls/reviews) to validate and post findings produced locally.
