import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import pr_reviewer as reviewer


ROOT = Path(__file__).resolve().parents[1]
REPO = "target/project"
NUMBER = 42
PULL = f"repos/{REPO}/pulls/{NUMBER}"
HEAD, BASE, NEXT_SHA = ("a" * 40, "b" * 40, "c" * 40)
ADD = "@@ -0,0 +1 @@\n+new"
DELETE = "@@ -1 +0,0 @@\n-old"
MULTI = (
    "@@ -2,3 +2,4 @@ first\n context\n-old\n+new\n+extra\n tail\n"
    "@@ -12,2 +13,2 @@ second\n retained\n-last\n+replacement\n"
    "\\ No newline at end of file\n"
)


def file_record(path, diff=ADD, additions=1, deletions=0):
    return {"path": path, "patch": diff, "additions": additions, "deletions": deletions}


def snapshot(*files, missing=0):
    return {
        "version": reviewer.VERSION,
        "metadata": {
            "repo": REPO, "pr": NUMBER, "head": HEAD, "base": BASE,
            "changed_files": len(files) + missing,
            "additions": sum(file["additions"] for file in files),
            "deletions": sum(file["deletions"] for file in files),
        },
        "files": list(files),
    }


def finding(path="src.py", line=1, body="Existing callers now receive an empty result."):
    return {"path": path, "line": line, "body": body}


class DiffTests(unittest.TestCase):
    def test_new_side_anchors_across_context_deletions_and_hunks(self):
        self.assertEqual(reviewer.added_lines(MULTI, 3, 2), {3, 4, 14})
        self.assertEqual(reviewer.added_lines(DELETE, 0, 1), set())
        self.assertEqual(reviewer.added_lines("@@ -0,0 +1 @@\n+++ literal", 1, 0), {1})

    def test_truncation_within_and_between_hunks(self):
        for diff in (MULTI.split("+replacement")[0], MULTI.split("@@ -12")[0]):
            with self.subTest(diff=diff), self.assertRaises(reviewer.ReviewError):
                reviewer.added_lines(diff, 3, 2)

    def test_malformed_or_inconsistent_hunks_are_rejected(self):
        candidates = (
            "@@ -0,0 +0 @@\n+zero",
            "@@ -0,0 +1 @@\n+a\n+b",
            "@@ -0,0 +1 @@\nmissing prefix",
            ADD + "\n@@ -0,0 +1 @@\n+overlap",
        )
        for diff in candidates:
            with self.subTest(diff=diff), self.assertRaises(reviewer.ReviewError):
                reviewer.added_lines(diff, 1, 0)

    def test_capture_discloses_the_retained_patch_budget(self):
        captured = snapshot(file_record("one.py"), file_record("two.py"))
        metadata = captured["metadata"]
        files = [
            {"filename": path, "patch": ADD, "additions": 1, "deletions": 0}
            for path in ("one.py", "two.py")
        ]
        with (
            patch.object(reviewer, "pr_metadata", return_value=(metadata, True)),
            patch.object(reviewer, "github", return_value=files),
            patch.object(reviewer, "MAX_RETAINED_PATCH_BYTES", len(reviewer.encode_json(ADD))),
        ):
            limited = reviewer.capture(REPO, NUMBER)
            _, coverage = reviewer.plan_review(limited, 1, reviewer.MAX_PROMPT_BYTES)
        self.assertEqual(coverage["reviewed_files"], 1)
        self.assertEqual(coverage["skipped"], {"capture budget": ["two.py"]})


class ReviewTests(unittest.TestCase):
    def test_strict_model_json(self):
        for raw in ('{"findings":[],"findings":[]}', '{"findings":NaN}', '```json\n{}\n```', '{} {}'):
            with self.subTest(raw=raw), self.assertRaisesRegex(reviewer.ReviewError, "Invalid JSON"):
                reviewer.decode_json(raw, "model")

    def test_exact_added_line_and_finding_fields_are_required(self):
        anchors = {"src.py": reviewer.added_lines(MULTI, 3, 2)}
        valid = finding(line=14)
        self.assertEqual(reviewer.validate_findings({"findings": [valid]}, anchors), [valid])
        invalid = [
            finding(path="./src.py", line=14), finding(path="Src.py", line=14),
            *(finding(line=line) for line in (2, 5, 12, 13, 0, -1, True, "14", 14.0)),
            finding(line=14, body="\t\x00"), finding(line=14, body="x" * (reviewer.MAX_BODY_CHARS + 1)),
            {**valid, "side": "LEFT"}, {"path": "src.py", "line": 14},
        ]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(reviewer.ReviewError):
                reviewer.validate_findings({"findings": [item]}, anchors)
        with self.assertRaises(reviewer.ReviewError):
            reviewer.validate_findings({"findings": [valid] * (reviewer.MAX_BATCH_FINDINGS + 1)}, anchors)

    def test_batches_are_sequential_and_disclose_all_coverage_gaps(self):
        files = [
            file_record("one.py", ADD + "<tag>é"), file_record("two.py"), file_record("three.py"),
            file_record("long.py", ADD + "x" * 1000), file_record("delete.py", DELETE, 0, 1),
            {"path": "missing.bin", "additions": 0, "deletions": 0, "skip": "patch absent"},
            {"path": "truncated.py", "additions": 3, "deletions": 2, "skip": "patch incomplete"},
        ]
        captured = snapshot(*files, missing=1)
        prompt_budget = len(reviewer.PROMPT) + 120
        seen = []

        def model(batch):
            entries = json.loads(batch.data)
            self.assertEqual(len(entries), 1)
            self.assertLessEqual(len(reviewer.PROMPT) + len(batch.data.encode("ascii")) + 32, prompt_budget)
            seen.append(entries[0])
            return {"findings": []}

        with patch.object(reviewer, "run_codex", side_effect=model):
            result = reviewer.review(captured, 2, prompt_budget)
        self.assertEqual(seen, [{"path": file["path"], "patch": file["patch"]} for file in files[:2]])
        self.assertEqual(result["coverage"], {
            "reviewed_files": 2,
            "unavailable_files": 1,
            "skipped": {
                "review budget": ["three.py"], "patch over prompt size limit": ["long.py"],
                "no added lines": ["delete.py"], "patch absent": ["missing.bin"],
                "patch incomplete": ["truncated.py"],
            },
        })
        payload = reviewer.build_payload(captured, result)
        self.assertEqual(payload["comments"], [])
        for phrase in ("2 of 8", "No qualifying findings", "did not return 1", *result["coverage"]["skipped"]):
            self.assertIn(phrase, payload["body"])

    def test_validation_rejects_findings_from_a_different_batch(self):
        captured = snapshot(file_record("first.py"), file_record("second.py"))
        with patch.object(reviewer, "run_codex", return_value={"findings": [finding("second.py")]}):
            with self.assertRaisesRegex(reviewer.ReviewError, "Review batch 1/2 failed"):
                reviewer.review(captured, 2, len(reviewer.PROMPT) + 100)

    def test_payload_deduplicates_caps_and_neutralizes_markdown(self):
        body = "@org/team ![img](https://example.invalid/i)\n[link](https://example.invalid) <a>\x00"
        captured = snapshot(file_record("src.py", MULTI, 3, 2))
        output = {"findings": [finding(line=3, body=body), finding(line=3), finding(line=4)]}
        with patch.object(reviewer, "run_codex", return_value=output):
            result = reviewer.review(captured, 1, reviewer.MAX_PROMPT_BYTES)
        self.assertEqual([item["line"] for item in reviewer.build_payload(captured, result)["comments"]], [3, 4])
        with patch.object(reviewer, "MAX_FINDINGS", 1):
            payload = reviewer.build_payload(captured, result)
        comment, = payload["comments"]
        self.assertEqual((comment["path"], comment["line"], comment["side"]), ("src.py", 3, "RIGHT"))
        self.assertIn("limit omitted 1 additional finding", payload["body"])
        self.assertIn("＠org/team", comment["body"])
        self.assertNotIn("@", comment["body"])
        self.assertNotIn("\x00", comment["body"])
        self.assertTrue(all(line.startswith("    ") for line in comment["body"].splitlines()))
        self.assertIn("![img]", comment["body"])
        self.assertIn("[link]", comment["body"])

    def test_smaller_hunks_preserve_added_line_anchors(self):
        captured = snapshot(file_record("src.py", MULTI, 3, 2))
        batches, _ = reviewer.plan_review(captured, 1, reviewer.MAX_PROMPT_BYTES)
        pieces = reviewer.split_batch(batches[0])
        self.assertEqual(set().union(*(part.anchors["src.py"] for part in pieces)),
                         batches[0].anchors["src.py"])
        varied = [child for piece in pieces for child in reviewer.split_batch(piece)]
        self.assertTrue(varied)
        self.assertTrue(all(child.anchors["src.py"] <= batches[0].anchors["src.py"] for child in varied))

    def test_model_budget_failure_splits_the_batch_and_combines_output(self):
        captured = snapshot(file_record("first.py", ADD, 1, 0), file_record("second.py", ADD, 1, 0))
        answers = [reviewer.RetryableReviewError("max_output_tokens"),
                   {"findings": [finding(path="first.py")]},
                   {"findings": [finding(path="second.py")]}]
        with patch.object(reviewer, "run_codex", side_effect=answers) as call:
            result = reviewer.review(captured, 1, reviewer.MAX_PROMPT_BYTES)
        self.assertEqual(call.call_count, 3)
        self.assertEqual([item["path"] for item in result["outputs"][0]["findings"]],
                         ["first.py", "second.py"])
        self.assertEqual(len(reviewer.build_payload(captured, result)["comments"]), 2)

    def test_checkpoint_resumes_successful_parts_and_saved_split(self):
        captured = snapshot(file_record("first.py", ADD, 1, 0), file_record("second.py", ADD, 1, 0))
        with tempfile.TemporaryDirectory() as folder:
            checkpoint = Path(folder) / "checkpoint.json"
            answers = [reviewer.RetryableReviewError("max_output_tokens"),
                       {"findings": [finding(path="first.py")]},
                       reviewer.ReviewError("model is busy")]
            with patch.object(reviewer, "run_codex", side_effect=answers):
                with self.assertRaisesRegex(reviewer.ReviewError, "model is busy"):
                    reviewer.review(captured, 1, reviewer.MAX_PROMPT_BYTES, checkpoint_path=checkpoint)
            with patch.object(reviewer, "run_codex", return_value={"findings": [finding(path="second.py")]}) as call:
                result = reviewer.review(captured, 1, reviewer.MAX_PROMPT_BYTES, checkpoint_path=checkpoint)
            self.assertEqual(call.call_count, 1)
        self.assertEqual(len(reviewer.build_payload(captured, result)["comments"]), 2)

    def test_no_reviewable_patches_does_not_claim_a_clean_review(self):
        captured = snapshot(file_record("delete.py", DELETE, 0, 1))
        with patch.object(reviewer, "run_codex") as model:
            result = reviewer.review(captured, 1, reviewer.MAX_PROMPT_BYTES)
        model.assert_not_called()
        payload = reviewer.build_payload(captured, result)
        self.assertEqual(payload["comments"], [])
        self.assertIn("0 of 1 changed files", payload["body"])
        self.assertNotIn("No qualifying findings", payload["body"])


FAKE_GH = r"""
import json
import os
from pathlib import Path
import sys

root = Path(__file__).parent
log = root / "gh.log"
calls = log.read_text().splitlines() if log.exists() else []
queue = json.loads((root / "gh.json").read_text())
args = sys.argv[1:]
raw = sys.stdin.read()
assert os.environ.get("GH_PROMPT_DISABLED") == "1"
assert "OPENAI_API_KEY" not in os.environ and "CODEX_API_KEY" not in os.environ
with log.open("a") as stream:
    print(json.dumps({"args": args, "input": json.loads(raw) if raw else None,
                      "token": os.environ.get("GH_TOKEN")}), file=stream)
expected, token, response = queue[len(calls)]
assert args == expected and os.environ.get("GH_TOKEN") == token, args
print(json.dumps(response))
"""

FAKE_CODEX = r"""
import json
import os
from pathlib import Path
import sys

root = Path(__file__).parent
args = sys.argv[1:]
work = Path.cwd()
def args_value(name):
    return args[args.index(name) + 1]

with (root / "codex.log").open("a") as stream:
    print(json.dumps({"args": args, "stdin": sys.stdin.read(), "env": dict(os.environ),
                      "cwd": str(work), "work_files": list(p.name for p in work.iterdir()),
                      "home_files": list(p.name for p in Path(os.environ["CODEX_HOME"]).iterdir()),
                      "schema": json.loads(Path(args_value("--output-schema")).read_text())}), file=stream)
fixture = root / "codex.json"
if fixture.exists():
    Path(args_value("--output-last-message")).write_bytes(fixture.read_bytes())
else:
    sys.exit(5)
"""


class OfflineIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.snapshot = self.root / "snapshot.json"
        self.result = self.root / "result.json"
        self.log = self.root / "gh.log"
        for name, source in (("gh", FAKE_GH), ("codex-inkling", FAKE_CODEX)):
            executable = self.root / name
            executable.write_text(f"#!{sys.executable}\n" + source)
            executable.chmod(0o755)
        (self.root / "python3").symlink_to(sys.executable)
        (self.root / "mkdir").symlink_to("/bin/mkdir")
        # The child can resolve only the explicitly installed test executables.
        self.env = {"PATH": str(self.root), "HOME": str(self.root), "LC_ALL": "C"}
        self.target = ("--repo", REPO, "--pr", str(NUMBER))
        self.output = self.root / "codex.json"
        self.write(self.output, {"findings": [finding()]})

    def write(self, path, data):
        path.write_text(json.dumps(data), encoding="ascii")

    def records(self, name):
        log = self.root / f"{name}.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def queue(self, *requests):
        self.log.unlink(missing_ok=True)
        self.write(self.root / "gh.json", requests)

    def run_command(self, argv, env, code=0):
        process = subprocess.run(argv, cwd=ROOT, env={**self.env, **env}, capture_output=True, text=True, timeout=15)
        self.assertEqual(process.returncode, code, process.stdout + process.stderr)
        return process

    def cli(self, command, *args, env=None, code=0):
        return self.run_command(
            [sys.executable, str(ROOT / "pr_reviewer.py"), command, "--snapshot", str(self.snapshot), *args],
            env or {}, code,
        )

    def pr(self, *, head=HEAD, base=BASE, state="open", draft=False, files=1, additions=1, deletions=0):
        return {
            "number": NUMBER, "state": state, "draft": draft, "changed_files": files, "additions": additions, "deletions": deletions,
            "head": {"sha": head, "repo": {"full_name": "fork/project", "fork": True}},
            "base": {"sha": base, "repo": {"full_name": REPO}},
        }

    def capture_queue(self, files, *, first=None, last=None, history=None):
        first = self.pr() if first is None else first
        last = first if last is None else last
        requests = [(["api", PULL], "reader", first)]
        if history is not None:
            requests.append((["api", f"{PULL}/reviews?per_page=100", "--paginate", "--slurp"], "reader", history))
        for offset in range(0, len(files), 100):
            page = offset // 100 + 1
            requests.append((["api", f"{PULL}/files?per_page=100&page={page}"], "reader", files[offset:offset + 100]))
        requests.append((["api", PULL], "reader", last))
        self.queue(*requests)

    def publish_queue(self, *, first=None, last=None, history=None, post=False):
        requests = [(["api", PULL], "writer", self.pr() if first is None else first)]
        if history is not None:
            requests.append((["api", f"{PULL}/reviews?per_page=100", "--paginate", "--slurp"], "writer", history))
        if last is not None:
            requests.append((["api", PULL], "writer", last))
        if post:
            requests.append((["api", f"{PULL}/reviews", "--method", "POST", "--input", "-"], "writer", {}))
        self.queue(*requests)

    def capture(self, code=0):
        return self.cli(
            "capture", *self.target, "--expected-head", HEAD, "--expected-base", BASE,
            env={"GH_TOKEN": "reader"}, code=code,
        )

    def generate(self, code=0):
        return self.cli("review", "--result", str(self.result), env={"OPENAI_API_KEY": "model-key"}, code=code)

    def publish(self, *args, code=0):
        return self.cli("publish", *self.target, "--result", str(self.result), *args, env={"GH_TOKEN": "writer"}, code=code)

    def capture_and_generate(self):
        self.capture_queue([{"filename": "src.py", "patch": ADD, "additions": 1, "deletions": 0}])
        self.capture()
        self.generate()

    def test_capture_paginates_and_preserves_metadata_and_coverage(self):
        files = [{"filename": f"binary-{index}", "additions": 0, "deletions": 0} for index in range(99)]
        files += [
            {"filename": "truncated.py", "patch": MULTI.split("@@ -12")[0], "additions": 3, "deletions": 2},
            {"filename": "large.py", "patch": ADD + "x" * reviewer.MAX_PATCH_BYTES, "additions": 1, "deletions": 0},
            {"filename": "src.py", "patch": ADD, "additions": 1, "deletions": 0},
        ]
        self.capture_queue(files, first=self.pr(files=103, additions=5, deletions=2))
        self.capture()
        captured = json.loads(self.snapshot.read_text())
        self.assertEqual(captured["metadata"], {
            "repo": REPO, "pr": NUMBER, "head": HEAD, "base": BASE,
            "changed_files": 103, "additions": 5, "deletions": 2,
        })
        self.assertEqual(reviewer.validate_snapshot(captured), {"src.py": {1}})
        self.assertEqual([item["skip"] for item in captured["files"][:-1]],
                         ["patch absent"] * 99 + ["patch incomplete", "patch over size limit"])
        self.assertEqual(len(self.records("gh")), 4)

    def test_capture_rejects_a_head_or_base_change_during_fetch(self):
        files = [{"filename": "src.py", "patch": ADD, "additions": 1, "deletions": 0}]
        for sha in ("head", "base"):
            with self.subTest(sha=sha):
                self.capture_queue(files, last=self.pr(**{sha: NEXT_SHA}))
                self.assertIn("changed during capture", self.capture(code=3).stderr)
                self.assertFalse(self.snapshot.exists())

    def test_capture_rejects_inactive_or_mismatched_event_before_fetch(self):
        for pr in (self.pr(draft=True), self.pr(state="closed"), self.pr(head=NEXT_SHA), self.pr(base=NEXT_SHA)):
            with self.subTest(pr=pr):
                self.queue((["api", PULL], "reader", pr))
                self.capture(code=3)
                self.assertEqual(len(self.records("gh")), 1)
                self.assertFalse(self.snapshot.exists())

    def test_capture_skips_existing_bot_review_before_fetching_patches(self):
        marker = reviewer.review_marker({"head": HEAD, "base": BASE})
        self.queue(
            (["api", PULL], "reader", self.pr()),
            (["api", f"{PULL}/reviews?per_page=100", "--paginate", "--slurp"], "reader",
             [[{"user": {"login": reviewer.BOT_LOGIN}, "body": marker}]]),
        )
        finished = self.cli("capture", *self.target, "--skip-reviewed",
                            env={"GH_TOKEN": "reader"}, code=3)
        self.assertIn("already has a bot review", finished.stderr)
        self.assertFalse(self.snapshot.exists())
        self.assertEqual(len(self.records("gh")), 2)

    def test_dry_run_ignores_spoofed_marker_and_other_base(self):
        self.capture_and_generate()
        marker = reviewer.review_marker({"head": HEAD, "base": BASE})
        old = reviewer.review_marker({"head": HEAD, "base": NEXT_SHA})
        self.publish_queue(last=self.pr(), history=[[
            {"user": {"login": "fork-author"}, "body": marker},
            {"user": {"login": reviewer.BOT_LOGIN}, "body": old},
            {"user": {"login": reviewer.BOT_LOGIN}, "body": f"quoted {marker}"},
        ]])
        payload = json.loads(self.publish("--dry-run").stdout)
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload["commit_id"], HEAD)
        self.assertIn(marker, payload["body"].splitlines())
        self.assertEqual(payload["comments"], [{
            "path": "src.py", "line": 1, "side": "RIGHT", "body": "    " + finding()["body"],
        }])
        self.assertEqual(len(self.records("gh")), 3)
        self.assertTrue(all(call["input"] is None for call in self.records("gh")))

    def test_publish_skips_stale_head_or_base_and_inactive_pr(self):
        self.capture_and_generate()
        for current in (self.pr(head=NEXT_SHA), self.pr(base=NEXT_SHA), self.pr(draft=True), self.pr(state="closed")):
            with self.subTest(current=current):
                self.publish_queue(first=current)
                self.assertIn("Skipping", self.publish().stderr)
                self.assertEqual(len(self.records("gh")), 1)
                self.publish_queue(history=[[]], last=current)
                self.assertIn("changed during publish checks", self.publish().stderr)
                self.assertEqual(len(self.records("gh")), 3)

    def test_publish_suppresses_matching_bot_review_on_later_page(self):
        self.capture_and_generate()
        marker = reviewer.review_marker({"head": HEAD, "base": BASE})
        self.publish_queue(history=[[], [{"user": {"login": reviewer.BOT_LOGIN}, "body": f"Summary\n\n{marker}"}]])
        self.assertIn("already reviewed", self.publish().stderr)
        self.assertEqual(len(self.records("gh")), 2)

    def test_publish_can_submit_a_summary_only_review(self):
        self.write(self.output, {"findings": []})
        self.capture_and_generate()
        self.publish_queue(history=[[]], last=self.pr(), post=True)
        self.publish()
        payload = self.records("gh")[-1]["input"]
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload["comments"], [])
        self.assertIn("No qualifying findings in the reviewed patches.", payload["body"])

    def test_failed_model_runs_do_not_create_a_result(self):
        self.capture_queue([{"filename": "src.py", "patch": ADD, "additions": 1, "deletions": 0}])
        self.capture()
        for raw, error in (
            (b"not JSON", "Invalid JSON"),
            (b" " * (reviewer.MAX_MODEL_BYTES + 1), "size limit"),
            (json.dumps({"findings": [finding(line=2)]}).encode("ascii"), "included added line"),
            (None, "execution failed"),
        ):
            with self.subTest(error=error):
                if raw is None:
                    self.output.unlink()
                else:
                    self.output.write_bytes(raw)
                self.assertIn(error, self.generate(code=1).stderr)
                self.assertFalse(self.result.exists())

    def test_failed_rerun_prevents_publishing_previous_clean_result(self):
        self.write(self.output, {"findings": []})
        self.capture_and_generate()
        self.publish_queue(history=[[]], last=self.pr())
        payload = json.loads(self.publish("--dry-run").stdout)
        self.assertIn("No qualifying findings in the reviewed patches.", payload["body"])

        self.output.unlink()
        self.assertIn("execution failed", self.generate(code=1).stderr)

        self.queue()
        rejected = self.publish("--dry-run", code=1)
        self.assertIn("Could not read review result", rejected.stderr)
        self.assertEqual(rejected.stdout, "")
        self.assertEqual(self.records("gh"), [])

    def test_publisher_revalidates_artifacts_before_calling_gh(self):
        self.capture_and_generate()
        original = json.loads(self.result.read_text())
        bad_id = {**original, "snapshot_id": "wrong"}
        bad_coverage = {**original, "coverage": {}}
        bad_anchor = copy.deepcopy(original)
        bad_anchor["outputs"][0]["findings"][0]["line"] = 2
        self.queue()
        for invalid, error in ((bad_id, "does not match"), (bad_coverage, "coverage"), (bad_anchor, "included added line")):
            with self.subTest(error=error):
                self.write(self.result, invalid)
                self.assertIn(error, self.publish(code=1).stderr)
                self.assertEqual(self.records("gh"), [])

    def test_local_poll_dispatches_and_workflow_posts_as_bot(self):
        workflow = (ROOT / ".github/workflows/codex-pr-review.yml").read_text()
        self.assertIn("repository_dispatch:\n    types: [inkling-pr-review-v1]", workflow)
        self.assertIn("permissions: {}", workflow)
        self.assertIn("permissions:\n      contents: read\n      pull-requests: write", workflow)
        self.assertIn("ref: ${{ github.workflow_sha }}", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertNotIn("OPENAI_API_KEY", workflow)
        self.assertNotIn("pull_request_target", workflow)
        command = workflow.split("        run: ", 1)[1].strip()
        self.assertNotIn("${{", command)

        path = "fork/odd name.py"
        diff = ADD + " </STDIN> $(printf patch) @someone"
        file = {"filename": path, "patch": diff, "additions": 1, "deletions": 0}
        self.write(self.output, {"findings": [finding(path=path)]})
        state_dir = self.root / "watcher"
        list_request = (["api", f"repos/{REPO}/pulls?state=open&per_page=100", "--paginate", "--slurp"],
                        "reader", [[self.pr(), {**self.pr(draft=True), "number": 2}]])
        reviews = f"{PULL}/reviews?per_page=100"
        self.queue(
            list_request,
            (["api", PULL], "reader", self.pr()),
            (["api", reviews, "--paginate", "--slurp"], "reader", [[]]),
            (["api", f"{PULL}/files?per_page=100&page=1"], "reader", [file]),
            (["api", PULL], "reader", self.pr()),
            (["api", f"repos/{REPO}/dispatches", "--method", "POST", "--input", "-"], "reader", None),
        )
        poll_args = [sys.executable, str(ROOT / "pr_reviewer.py"), "poll", "--repo", REPO,
                     "--state-dir", str(state_dir)]
        self.env.update(GITHUB_TOKEN="ambient", GH_ENTERPRISE_TOKEN="ambient", ACTIONS_RUNTIME_TOKEN="ambient")
        self.run_command(poll_args, {"GH_TOKEN": "reader"})
        envelope = self.records("gh")[-1]["input"]
        self.assertEqual(envelope["event_type"], reviewer.DISPATCH_EVENT)

        model, = self.records("codex")
        self.assertEqual(json.loads(model["stdin"]), [{"path": path, "patch": diff, "allowed_added_lines": [1]}])
        self.assertNotIn("</STDIN>", model["stdin"])
        self.assertNotIn(path, " ".join(model["args"]))
        self.assertEqual(model["work_files"], [])
        self.assertEqual(model["home_files"], [])
        self.assertFalse(Path(model["cwd"]).exists())
        self.assertEqual(model["env"]["HOME"], str(self.root))
        self.assertEqual(set(model["env"]) - {"__CF_USER_TEXT_ENCODING"},
                         {"PATH", "HOME", "LC_ALL", "CODEX_HOME", "TMPDIR", "HF_HUB_OFFLINE", "INKLING_REASONING_EFFORT", "INKLING_TEMPERATURE"})
        args = model["args"]
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        self.assertTrue({"--ignore-user-config", "--ignore-rules", "--ephemeral"}.issubset(args))

        self.queue(list_request)
        self.run_command(poll_args, {"GH_TOKEN": "reader"})
        self.assertEqual(len(self.records("gh")), 1)
        self.assertEqual(self.records("codex"), [model])

        event = self.root / "event.json"
        self.write(event, {"action": reviewer.DISPATCH_EVENT, "client_payload": envelope["client_payload"]})
        self.queue(
            (["api", PULL], "writer", self.pr()),
            (["api", reviews, "--paginate", "--slurp"], "writer", [[]]),
            (["api", f"{PULL}/files?per_page=100&page=1"], "writer", [file]),
            (["api", PULL], "writer", self.pr()),
            (["api", PULL], "writer", self.pr()),
            (["api", reviews, "--paginate", "--slurp"], "writer", [[]]),
            (["api", PULL], "writer", self.pr()),
            (["api", f"{PULL}/reviews", "--method", "POST", "--input", "-"], "writer", {}),
        )
        publish_env = {"GH_TOKEN": "writer", "GITHUB_REPOSITORY": REPO, "GITHUB_EVENT_PATH": str(event)}
        self.run_command(["/bin/sh", "-eu", "-c", command], publish_env)
        reads = self.records("gh")
        self.assertTrue(all(call["input"] is None for call in reads[:-1]))
        self.assertEqual(reads[-1]["input"]["event"], "COMMENT")
        self.assertEqual(reads[-1]["input"]["comments"],
                         [{"path": path, "line": 1, "side": "RIGHT", "body": "    " + finding()["body"]}])

        self.queue((["api", PULL], "writer", self.pr(head=NEXT_SHA)))
        self.run_command(["/bin/sh", "-eu", "-c", command], publish_env)
        self.assertEqual(len(self.records("gh")), 1)
        self.assertEqual(self.records("codex"), [model])


if __name__ == "__main__":
    unittest.main()
