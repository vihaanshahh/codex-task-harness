import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HARNESS = Path(__file__).resolve().parents[1] / "codex_task.py"
FAKE_CODEX = r'''
import os
from pathlib import Path
import sys

prompt = sys.stdin.read()
dest = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
work = Path("src.txt")
scenario = os.environ["HARNESS_SCENARIO"]
if dest.name.startswith("plan-"):
    result = '{"steps":["update the scoped file"]}'
elif dest.name.startswith("review-"):
    result = '{"verdict":"pass","summary":"The expected change is present."}'
elif dest.name.startswith("repair-"):
    work.write_text("fixed")
    result = "Repaired"
elif "Inspect and report findings." in prompt:
    result = "Read-only inspection completed"
else:
    if scenario == "interrupt" and dest.name == "step-1-1.txt":
        work.write_text("partial")
        sys.exit(1)
    if scenario == "outside":
        Path("different.txt").write_text("outside")
    work.write_text("wrong" if scenario == "repair" else "fixed")
    result = "Changed the file"
dest.write_text(result)
'''


class HarnessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.fake = self.root / "fake-codex"
        self.fake.write_text(f"#!{sys.executable}\n" + FAKE_CODEX)
        self.fake.chmod(0o755)
        self.git("init", "-q")
        (self.repo / "src.txt").write_text("base")
        (self.repo / "different.txt").write_text("other")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def cli(self, *args, code=0, scenario="pass"):
        run = subprocess.run(
            [sys.executable, str(HARNESS), "--state-dir", str(self.state), *args],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "HARNESS_SCENARIO": scenario},
        )
        self.assertEqual(run.returncode, code, run.stdout + run.stderr)
        return run.stdout.strip()

    def enqueue(self, *checks, repairs=0, mode="edit"):
        task = self.root / "task.toml"
        task.write_text(
            'task = "Fix src.txt"\nscope = ["src.txt"]\n'
            f'mode = "{mode}"\nchecks = {json.dumps(checks)}\n'
            f"repair_attempts = {repairs}\ntimeout_seconds = 5\n"
        )
        return self.cli("enqueue", str(task), "--repo", str(self.repo), "--codex", str(self.fake))

    def show(self, job):
        return json.loads(self.cli("show", job))

    def verification(self):
        return [sys.executable, "-c", "from pathlib import Path; assert Path('src.txt').read_text() == 'fixed'"]

    def test_pins_commit_and_preserves_dirty_checkout(self):
        base = self.git("rev-parse", "HEAD")
        job = self.enqueue(self.verification())
        (self.repo / "src.txt").write_text("different revision")
        self.git("add", "src.txt")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "next")
        (self.repo / "src.txt").write_text("my dirty changes")
        self.cli("run")
        state = self.show(job)
        self.assertEqual(state["status"], "passed")
        self.assertEqual(state["base"], base)
        self.assertEqual(state["changed_paths"], ["src.txt"])
        self.assertEqual((self.repo / "src.txt").read_text(), "my dirty changes")

    def test_repairs_failed_verification_and_reruns_it(self):
        job = self.enqueue(self.verification(), repairs=1)
        self.cli("run", scenario="repair")
        state = self.show(job)
        self.assertEqual(state["steps"]["check-0-1"]["status"], "failed")
        self.assertEqual(state["steps"]["check-1-1"]["status"], "passed")
        self.assertEqual(state["status"], "passed")

    def test_scope_violation_blocks_the_job(self):
        job = self.enqueue(self.verification())
        self.cli("run", scenario="outside", code=1)
        state = self.show(job)
        self.assertEqual(state["status"], "blocked")
        self.assertIn("different.txt", state["error"])
        self.assertEqual((self.repo / "different.txt").read_text(), "other")

    def test_check_cannot_rewrite_a_scoped_file_and_claim_success(self):
        job = self.enqueue([sys.executable, "-c", "from pathlib import Path; Path('src.txt').write_text('wrong')"])
        self.cli("run", code=1)
        state = self.show(job)
        self.assertEqual(state["status"], "blocked")
        self.assertIn("Verification command 1 changed the checkout", state["error"])

    def test_retry_keeps_completed_plan(self):
        job = self.enqueue(self.verification())
        self.cli("run", scenario="interrupt", code=1)
        self.assertEqual(self.show(job)["status"], "blocked")
        self.cli("run", "--job", job, "--retry", scenario="interrupt")
        state = self.show(job)
        self.assertEqual(state["status"], "passed")
        self.assertEqual(state["attempts"]["plan"], 1)
        self.assertEqual(state["attempts"]["step-1"], 2)

    def test_review_mode_keeps_checkout_clean(self):
        job = self.enqueue([sys.executable, "-c", "raise SystemExit(0)"], mode="review")
        self.cli("run")
        state = self.show(job)
        self.assertEqual(state["status"], "passed")
        self.assertEqual(state["changed_paths"], [])

    def test_worker_lock_rejects_another_drain(self):
        job = self.enqueue(self.verification())
        with (self.state / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.cli("run", code=2)
        self.assertEqual(self.show(job)["status"], "queued")

    def test_report_never_claims_pass_without_checks(self):
        job = self.enqueue()
        self.cli("run", code=1)
        self.assertEqual(self.show(job)["status"], "unverified")

    def test_model_pass_cannot_override_failed_check(self):
        job = self.enqueue([sys.executable, "-c", "raise SystemExit(3)"])
        self.cli("run", code=1)
        state = self.show(job)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["checks"][0]["exit_code"], 3)


if __name__ == "__main__":
    unittest.main()
