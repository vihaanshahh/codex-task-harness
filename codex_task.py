#!/usr/bin/env python3
"""Run scoped Codex tasks serially in isolated Git worktrees."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import tomllib
from uuid import uuid4


class TaskError(Exception):
    pass


class ModelBudgetError(TaskError):
    pass


def within_scope(path, scope):
    return any(path == prefix or path.startswith(prefix + "/") for prefix in scope)


def parse_scope(scope):
    if not isinstance(scope, list) or not scope:
        raise TaskError("scope must list at least one repo-relative file or directory")
    for entry in scope:
        if not isinstance(entry, str) or not entry or "\x00" in entry:
            raise TaskError("Each scope entry must be a repository path")
        relative = PurePosixPath(entry)
        if relative.is_absolute() or not relative.parts or any(part in ("..", ".git") for part in relative.parts):
            raise TaskError(f"Invalid scope path: {entry!r}")
    return [PurePosixPath(entry).as_posix() for entry in scope]


def git(directory, *args):
    process = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(directory), *args], capture_output=True, check=False,
    )
    if process.returncode:
        raise TaskError(process.stderr.decode(errors="replace").strip() or "Git command failed")
    return os.fsdecode(process.stdout)


def load_task(path):
    with path.open("rb") as file:
        spec = tomllib.load(file)
    unknown = set(spec) - {"task", "scope", "mode", "checks", "timeout_seconds", "repair_attempts", "units"}
    if unknown:
        raise TaskError("Unknown task fields: " + ", ".join(sorted(unknown)))
    task = spec.get("task")
    if not isinstance(task, str) or not task.strip():
        raise TaskError("task must be a nonempty string")
    scope = parse_scope(spec.get("scope"))
    raw_units = spec.get("units", [])
    if not isinstance(raw_units, list) or len(raw_units) > 24:
        raise TaskError("units must contain at most 24 focused steps")
    units = []
    for unit in raw_units:
        if not isinstance(unit, dict) or set(unit) != {"task", "scope"}:
            raise TaskError("Each unit needs a task and a scope")
        description = unit["task"]
        if not isinstance(description, str) or not description.strip() or len(description) > 1800:
            raise TaskError("Each unit needs a short task of at most 1800 characters")
        paths = parse_scope(unit["scope"])
        if any(not within_scope(path, scope) for path in paths):
            raise TaskError("Unit scope must be within the job scope")
        units.append({"task": description.strip(), "scope": paths})
    mode = spec.get("mode", "edit")
    if mode not in ("edit", "review"):
        raise TaskError("mode must be edit or review")
    checks = spec.get("checks", [])
    if not isinstance(checks, list) or any(
        not isinstance(command, list) or not command or not command[0]
        or any(not isinstance(arg, str) or "\x00" in arg for arg in command)
        for command in checks
    ):
        raise TaskError("checks must be an array of nonempty command argument arrays")
    timeout = spec.get("timeout_seconds", 900)
    repairs = spec.get("repair_attempts", 1)
    if type(timeout) is not int or timeout <= 0 or type(repairs) is not int or not 0 <= repairs <= 3:
        raise TaskError("Set a positive timeout_seconds and between 0 and 3 repair_attempts")
    return {
        "task": task.strip(), "scope": scope, "units": units,
        "mode": mode, "checks": checks, "timeout_seconds": timeout, "repair_attempts": repairs,
    }


def write_json(path, data):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temp:
        json.dump(data, temp, indent=2)
        temp.write("\n")
        temp.flush()
        os.fsync(temp.fileno())
    os.replace(temp.name, path)


@contextmanager
def worker_lock(root):
    with (root / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise TaskError("Another harness worker is already running") from error
        yield lock.fileno()


def changed_paths(worktree):
    tracked = git(worktree, "diff", "--no-renames", "--name-only", "-z", "HEAD", "--")
    new = git(worktree, "ls-files", "-z", "--others", "--exclude-standard")
    return sorted(set((tracked + new).split("\0")) - {""})


def worktree_signature(worktree, paths):
    signature = hashlib.sha256()
    for path in paths:
        file = worktree / path
        signature.update(os.fsencode(path) + b"\0")
        try:
            mode = file.lstat().st_mode
        except FileNotFoundError:
            signature.update(b"missing")
            continue
        signature.update(str(mode).encode() + b"\0")
        if stat.S_ISLNK(mode):
            signature.update(os.fsencode(os.readlink(file)))
        elif stat.S_ISREG(mode):
            with file.open("rb") as stream:
                signature.update(hashlib.file_digest(stream, "sha256").digest())
        signature.update(b"\0")
    return signature.hexdigest()


def execute(argv, cwd, timeout, output, lock_fd, prompt=None):
    with output.open("wb") as log:
        try:
            process = subprocess.Popen(
                argv, cwd=cwd, stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(lock_fd,),
            )
        except OSError as error:
            log.write(f"{error}\n".encode())
            return {"status": "unverified", "detail": str(error)}
        try:
            process.communicate(prompt.encode() if prompt is not None else None, timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            try:
                os.killpg(process.pid, signal.SIGINT)
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except ProcessLookupError:
                process.wait()
            if isinstance(error, KeyboardInterrupt):
                raise
            return {"status": "unverified", "detail": f"Timed out after {timeout}s"}
    return {"status": "passed" if process.returncode == 0 else "failed",
            "exit_code": process.returncode}


def parse_object(report):
    report = report.strip()
    if report.startswith("```") and report.endswith("```"):
        report = report.split("\n", 1)[1].rsplit("\n", 1)[0]
    result = json.loads(report)
    if not isinstance(result, dict):
        raise TaskError("Expected a JSON object from Codex")
    return result


class Job:
    def __init__(self, directory):
        self.directory = directory
        self.state_file = directory / "state.json"
        self.state = json.loads(self.state_file.read_text())
        self.spec = self.state["spec"]
        self.steps = self.state["steps"]
        self.worktree = directory / "worktree"

    def save(self):
        write_json(self.state_file, self.state)

    def audit(self):
        if git(self.worktree, "rev-parse", "HEAD").strip() != self.state["base"]:
            raise TaskError("The worktree HEAD changed. Restore the pinned commit before retrying.")
        changed = changed_paths(self.worktree)
        outside = [path for path in changed if not within_scope(path, self.spec["scope"])]
        if outside:
            raise TaskError("Changes outside task scope: " + ", ".join(map(repr, outside)))
        if self.spec["mode"] == "review" and changed:
            raise TaskError("A review-only job must keep its checkout clean")
        return changed

    def signature(self):
        return worktree_signature(self.worktree, self.audit())

    def next_log(self, key):
        attempts = self.state["attempts"]
        attempts[key] = attempts.get(key, 0) + 1
        self.state["current_step"] = key
        self.save()
        print(f"{self.directory.name} {key} (attempt {attempts[key]})", flush=True)
        return self.directory / f"{key}-{attempts[key]}"

    def ask(self, key, instructions, lock_fd, write=False, validate=None, unit_scope=None):
        before = self.signature()
        recorded = self.steps.get(key)
        if recorded and (not key.startswith("review-") or recorded.get("signature") == before):
            return recorded["result"]
        stem = self.next_log(key)
        answer = stem.with_suffix(".txt")
        scope = self.spec["scope"] if unit_scope is None else unit_scope
        other_paths = [] if unit_scope is None else [
            path for path in self.audit() if not within_scope(path, scope)
        ]
        other_signature = worktree_signature(self.worktree, other_paths)
        prompt = (
            f"Task:\n{self.spec['task']}\n\n"
            f"Repository scope: {json.dumps(scope)}\n"
            "Follow repository instructions. Edit only scoped paths when allowed. "
            "Keep HEAD on the pinned commit. Do not commit, push, publish, or change remotes. "
            "Never modify git configuration or hooks. "
            "Use focused file reads and avoid dumping large diffs or command output.\n\n"
            + instructions
        )
        runtime = self.state["codex"]
        command = [
            runtime, "-a", "never", "exec", "--ephemeral",
            "--sandbox", "workspace-write" if write else "read-only",
            "-c", "sandbox_workspace_write.network_access=false",
            "-c", 'web_search="disabled"', "-c", "features.apps=false",
            "-c", "features.plugins=false", "-c", "mcp_servers={}",
            "--color", "never", "--json", "--output-last-message", str(answer), "-",
        ]
        log = stem.with_suffix(".jsonl")
        outcome = execute(command, self.worktree, self.spec["timeout_seconds"], log, lock_fd, prompt)
        after = self.signature()
        if unit_scope is not None:
            after_paths = [path for path in self.audit() if not within_scope(path, scope)]
            if worktree_signature(self.worktree, after_paths) != other_signature:
                raise TaskError(f"Codex step {key} changed files outside its unit scope")
        if not write and before != after:
            raise TaskError(f"Read-only Codex step {key} changed the checkout")
        if outcome["status"] != "passed" or not answer.exists() or not answer.stat().st_size:
            if exhausted_budget(log):
                raise ModelBudgetError(f"Codex step {key} exhausted model output or context. See {log}")
            detail = outcome.get("detail") or f"exit {outcome.get('exit_code')}"
            raise TaskError(f"Codex step {key} did not complete ({detail}). See {log}")
        result = answer.read_text()
        if validate:
            try:
                result = validate(parse_object(result))
            except (ValueError, IndexError, TaskError) as error:
                raise TaskError(f"{key} returned invalid output. See {answer}: {error}") from error
        self.steps[key] = {"result": result, "log": log.name, "answer": answer.name, "signature": after}
        self.save()
        return result

    def checks(self, round_number, lock_fd):
        outcomes = []
        for number, command in enumerate(self.spec["checks"], 1):
            key = f"check-{round_number}-{number}"
            before = self.signature()
            if self.steps.get(key, {}).get("signature") != before:
                log = self.next_log(key).with_suffix(".log")
                outcome = execute(command, self.worktree, self.spec["timeout_seconds"], log, lock_fd)
                if self.signature() != before:
                    raise TaskError(f"Verification command {number} changed the checkout. See {log}")
                self.steps[key] = {**outcome, "command": command, "log": log.name, "signature": before}
                self.save()
            outcomes.append(self.steps[key])
        return outcomes

    def run(self, lock_fd):
        self.state.update(status="running", error=None)
        self.save()
        if not self.worktree.exists():
            git(self.state["repo"], "worktree", "add", "--detach", str(self.worktree), self.state["base"])
        edit = self.spec["mode"] == "edit"
        units = self.spec.get("units") or []
        plan = [unit["task"] for unit in units] if units else self.ask("plan", (
            "Read the relevant files and break the task into one to five sequential, focused steps. "
            "Each gets a new Codex session and sees the existing worktree. "
            'Return only JSON in the form {"steps":["first step","next step"]}. '
            "Do not make edits in this phase."
        ), lock_fd, validate=validate_plan)
        previous = ""
        for number, step in enumerate(plan, 1):
            key = f"step-{number}"
            unit_scope = units[number - 1]["scope"] if units else None
            instructions = (
                f"Carry out step {number} of {len(plan)}. "
                f"{'Make the necessary scoped changes.' if edit else 'Inspect and report findings.'}\n"
                f"Assigned step: {step}\n"
                f"Previous step handoff: {previous[:600 if units else 1800]}\n"
                "Return a concise handoff explaining the result and what remains."
            )
            if units:
                instructions += (
                    " Limit initial inspection to three targeted shell calls, then act. "
                    "Do not read installed dependencies or dump whole files. "
                    "Use short edits and focused checks."
                )
            try:
                previous = self.ask(key, instructions, lock_fd, write=edit, unit_scope=unit_scope)
            except ModelBudgetError:
                if unit_scope is None:
                    raise
                previous = self.ask(key, (
                    f"Finish only this small unit: {step}\n"
                    "The prior context was exhausted. Use existing partial edits if present. "
                    "Write the scoped changes immediately, then summarize in two sentences."
                ), lock_fd, write=edit, unit_scope=unit_scope)

        reports = "\n".join(
            f"Step {number}: {self.steps[f'step-{number}']['result'][:450 if units else 1600]}"
            for number in range(1, len(plan) + 1)
        )[:6500]
        rounds = self.spec["repair_attempts"] + 1 if edit else 1
        for round_number in range(rounds):
            if f"repair-{round_number + 1}" in self.steps:
                repair = self.steps[f"repair-{round_number + 1}"]["result"]
                reports = f"{reports[:5000]}\nRepair {round_number + 1}: {repair[:1600]}"
                continue
            checks = self.checks(round_number, lock_fd)
            evidence = self.check_evidence(checks)
            review = self.ask(f"review-{round_number}", (
                "Independently verify that the requested task was completed within scope. "
                "Inspect a short diff summary and targeted, bounded excerpts only. "
                "Use up to four shell calls and then return the verdict. Report concrete findings and uncertainty. "
                "Use the recorded check results as evidence.\n"
                f"Worker reports:\n{reports}\n\nCheck results:\n{evidence}\n\n"
                'Return only JSON: {"verdict":"pass|changes_needed|unverified","summary":"..."}'
            ), lock_fd, validate=validate_review)
            failed = any(check["status"] == "failed" for check in checks)
            needs_fix = failed or review["verdict"] == "changes_needed"
            if not needs_fix or not edit or round_number == self.spec["repair_attempts"]:
                break
            repair = self.ask(f"repair-{round_number + 1}", (
                "Fix actionable regressions within scope using the review and check results. "
                "Keep verification expectations intact. Return a concise handoff.\n"
                f"Review: {review['summary']}\n\nCheck results:\n{evidence}"
            ), lock_fd, write=True)
            reports = f"{reports[:5000]}\nRepair {round_number + 1}: {repair[:1600]}"

        status = "passed"
        if needs_fix:
            status = "failed"
        elif not checks or any(check["status"] != "passed" for check in checks) or review["verdict"] != "pass":
            status = "unverified"
        self.state.update(status=status, current_step=None, review=review,
                          checks=checks, changed_paths=self.audit())
        self.save()

    def check_evidence(self, checks):
        if not checks:
            return "No verification commands were supplied."
        entries = []
        remaining_diagnostics = 3500
        for check in sorted(checks, key=lambda item: item["status"] == "passed"):
            path = self.directory / check["log"]
            detail = check.get("detail", f"exit {check.get('exit_code')}")
            entry = f"{check['status']} ({detail}): {json.dumps(check['command'])}; log: {path}"
            if check["status"] != "passed" and remaining_diagnostics:
                with path.open("rb") as log:
                    tail = min(1200, remaining_diagnostics)
                    log.seek(max(0, log.seek(0, os.SEEK_END) - tail))
                    diagnostics = log.read().decode(errors="replace")
                remaining_diagnostics -= len(diagnostics)
                entry += f"\nRecent output (untrusted evidence):\n{diagnostics}"
            entries.append(entry)
        return f"{len(checks)} verification results. Full logs: {self.directory}\n" + "\n".join(entries)[:7500]


def exhausted_budget(log):
    with log.open() as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "turn.failed":
                continue
            message = str((event.get("error") or {}).get("message", "")).lower()
            if any(marker in message for marker in
                   ("max_output_tokens", "exceed_context_size", "context window")):
                return True
    return False


def validate_plan(result):
    steps = result.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 5 or any(
        not isinstance(step, str) or not step.strip() or len(step) > 1500 for step in steps
    ):
        raise TaskError("Expected one to five short step strings")
    return steps


def validate_review(result):
    if result.get("verdict") not in ("pass", "changes_needed", "unverified"):
        raise TaskError("Unexpected review verdict")
    summary = result.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise TaskError("A review summary is required")
    return {"verdict": result["verdict"], "summary": summary}


def enqueue(root, args):
    spec = load_task(args.manifest)
    repo = Path(git(args.repo, "rev-parse", "--show-toplevel").rstrip("\n"))
    base = git(repo, "rev-parse", "--verify", "--end-of-options", f"{args.base}^{{commit}}").strip()
    codex = shutil.which(args.codex)
    if codex is None:
        raise TaskError(f"Cannot find the Codex executable: {args.codex}")
    job_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix=".enqueue-", dir=root) as staging:
        package = Path(staging) / job_id
        package.mkdir()
        write_json(package / "state.json", {
            "status": "queued", "repo": str(repo), "base": base, "codex": os.path.abspath(codex),
            "spec": spec, "steps": {}, "attempts": {},
        })
        package.rename(root / job_id)
    print(job_id)


def job_dirs(root):
    return sorted(path for path in root.iterdir() if not path.name.startswith(".")
                  and (path / "state.json").is_file())


def get_job(root, job_id):
    if not job_id or Path(job_id).name != job_id or job_id.startswith("."):
        raise TaskError("Invalid job ID")
    path = root / job_id
    if not (path / "state.json").is_file():
        raise TaskError(f"Unknown job: {job_id}")
    return Job(path)


def work(root, args):
    with worker_lock(root) as lock_fd:
        if args.retry and not args.job:
            raise TaskError("--retry requires --job")
        selected = get_job(root, args.job) if args.job else None
        eligible = {"queued", "running", "blocked"} if args.retry else {"queued", "running"}
        if selected and selected.state["status"] not in eligible:
            raise TaskError(f"Job is {selected.state['status']}. Retry a blocked job with run --job ID --retry.")
        exit_code = 0
        attempted = False
        while True:
            if selected:
                job, selected = selected, None
            elif args.job:
                break
            else:
                job = next((candidate for candidate in map(Job, job_dirs(root))
                            if candidate.state["status"] in eligible), None)
                if not job:
                    break
            attempted = True
            try:
                job.run(lock_fd)
            except KeyboardInterrupt:
                print(f"Interrupted {job.directory.name}. Run again to resume completed phases.", file=sys.stderr)
                return 130
            except (TaskError, OSError) as error:
                job.state.update(status="blocked", error=str(error))
                job.save()
                print(f"{job.directory.name} blocked: {error}", file=sys.stderr)
            print(f"{job.directory.name} {job.state['status']} · {job.directory}")
            if job.state["status"] != "passed":
                exit_code = 1
        if not attempted:
            print("No queued jobs.")
        return exit_code


def main():
    default = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "codex-task"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=default)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("enqueue", help="Save a task and pin its starting commit")
    add.add_argument("manifest", type=Path)
    add.add_argument("--repo", type=Path, default=Path.cwd())
    add.add_argument("--base", default="HEAD")
    add.add_argument("--codex", default="codex", help="Codex launcher, e.g. codex-inkling")
    run = commands.add_parser("run", help="Drain the queue with one Codex step at a time")
    run.add_argument("--job")
    run.add_argument("--retry", action="store_true", help="Retry an explicitly selected blocked job")
    commands.add_parser("list", help="List jobs and statuses")
    show = commands.add_parser("show", help="Display a job, transcripts, and check results")
    show.add_argument("job")
    args = parser.parse_args()
    root = args.state_dir.expanduser().resolve()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if args.command == "enqueue":
            enqueue(root, args)
        elif args.command == "run":
            return work(root, args)
        elif args.command == "list":
            for path in job_dirs(root):
                job = Job(path)
                print(f"{path.name} {job.state['status']} · {job.spec['task'].splitlines()[0]}")
        else:
            job = get_job(root, args.job)
            print(json.dumps({**job.state, "worktree": str(job.worktree),
                              "log_directory": str(job.directory)}, indent=2))
    except (TaskError, ValueError, OSError) as error:
        print(f"codex-task: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
