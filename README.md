# Codex Task Harness

Queue scoped coding tasks and run them through Codex from planning to verification. The harness runs one phase at a time. Each Codex phase starts a fresh session, so the same queue works with a regular Codex login or a single local model such as Inkling.

## Install

Requires Git, Python 3.11+, and a working Codex CLI. With [uv](https://docs.astral.sh/uv/):

```sh
uv tool install git+https://github.com/vihaanshahh/codex-task-harness.git
```

From a local clone, run `uv tool install .` or use `python3.11 codex_task.py`.

## Submit work

Create a TOML manifest, then enqueue it from the repository you want to work on.

```toml
task = """
Fix the date parser so February 30 returns an invalid-date error.
Preserve valid dates and add a regression test.
"""
scope = ["src/date_parser.py", "tests/test_date_parser.py"]
mode = "edit"
checks = [["python3", "-m", "unittest", "discover", "-v"]]
timeout_seconds = 900
repair_attempts = 1
```

```sh
codex-task enqueue /path/to/task.toml
codex-task run
codex-task list
codex-task show JOB_ID
```

Enqueue several manifests before running to process them in order. Use `enqueue --repo /path/to/repo` to submit from another directory. `--base REF` pins a different local Git revision. `--codex codex-inkling` selects the existing Inkling launcher on a machine where it is separately installed. Use `mode = "review"` for a read-only inspection.

For a local model, you can supply ordered `[[units]]` with smaller task and path scopes. Each unit starts a fresh session, skips the planning call, and gets one short retry after an output or context limit. Whole-job checks and independent review still run afterward.

```toml
[[units]]
task = "Implement the date validity check in the parser."
scope = ["src/date_parser.py"]

[[units]]
task = "Add a regression test for February 30."
scope = ["tests/test_date_parser.py"]
```

Scope entries are literal repository-relative files or directories. Codex can read surrounding code. The harness checks tracked and non-ignored changes against the scope after each phase. It checks that read-only phases and verification commands preserve tracked and non-ignored file contents. Ignored build artifacts may be created. The selected repository and verification commands should be ones you trust. Verification commands run directly under your account with the supplied argument lists.

## What a job does

1. Pins the starting commit and creates a detached Git worktree. Any uncommitted changes in the original checkout stay there.
2. Asks Codex to plan up to five focused steps, then runs each step serially.
3. Runs your verification commands and records their exit codes and logs.
4. Runs an independent read-only review. When checks fail or the reviewer requests changes, an edit job can run a bounded repair pass and recheck.
5. Keeps the worktree, local logs, and result. The harness doesn't commit, push, or open PRs in task repositories.

`passed` requires at least one successful verification command, all configured checks passing, and a passing review. A nonzero check or unresolved review finding yields `failed`. Missing commands, timeouts, or missing verification evidence yield `unverified` when no failure is established. A Codex execution problem, invalid response, or scope violation yields `blocked`. The queue command exits nonzero unless every attempted job passes.

Inspect the retained worktree and transcripts with `codex-task show JOB_ID`. Job state lives outside the target repository at `${XDG_STATE_HOME:-$HOME/.local/state}/codex-task` by default. It can contain task prompts and source excerpts, so treat it as local project data.

After Ctrl-C, rerun `codex-task run` to continue from saved checkpoints. An interrupted step starts again and sees any partial changes in its worktree. After resolving a blocker, use `codex-task run --job JOB_ID --retry`. Completed checks and review are tied to the worktree content. The harness takes a local lock while a worker runs.

## Automatic PR review

Run the local Inkling worker to review open PRs one at a time. GitHub Actions validates the finished findings and posts them as a bot review. The [reviewer guide](docs/pr-reviewer.md) covers setup, dry runs, and review limits.

## Develop

```sh
python3.11 -m unittest discover -s tests -v
```
