import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

CLI = Path(__file__).resolve().parents[1] / "lean.py"


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (["init", "-q"], ["config", "user.email", "test@example.invalid"],
                     ["config", "user.name", "Harness test"]):
            self.git(*args)
        (self.repo / "source.txt").write_text("original\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.state = self.root / "session.json"
        self.contract = self.root / "contract.json"

    def git(self, *args, repo=None):
        return subprocess.check_output(["git", "-C", str(repo or self.repo), *args], stderr=subprocess.DEVNULL)

    def call(self, *args, code=0):
        p = subprocess.run([sys.executable, str(CLI), "--state", str(self.state), *map(str, args)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        return json.loads(p.stdout or p.stderr)

    def setup_task(self, script="print('ok')", timeout=3, **limits):
        argv = ["init", "--minutes", "10", "--execution-seconds", "20", "--helpers", "2"]
        for k, v in limits.items(): argv += ["--" + k.replace("_", "-"), str(v)]
        self.call(*argv)
        c = {"goal": "Check fixture behavior", "owner_request": "test fixture",
             "mode": "local", "checks": {"test": {"argv": [sys.executable, "-c", script],
                    "timeout_seconds": timeout}}, "required_external": [], "must_happen": ["check passes"],
             "must_never": ["silently bypass failed checks"], "non_goals": ["publish"],
             "policy_sources": ["fixture instructions"]}
        self.contract.write_text(json.dumps(c))
        self.call("start", "--task", "one", "--repo", self.repo, "--contract", self.contract)
        return c

    def run_check(self, code=0):
        return self.call("run", "--task", "one", "--check", "test", code=code)

    def assess(self):
        return self.call("assess", "--task", "one", "--outcome", "met", "--evidence", "Fixture accepted")

    def gate(self, code=1):
        return self.call("gate", "--task", "one", code=code)

    def test_success_requires_checks_and_scope(self):
        self.setup_task()
        self.assertEqual(self.gate()["validation"], "INCONCLUSIVE")
        self.run_check()
        self.assertEqual(self.gate()["validation"], "INCONCLUSIVE")
        self.assess()
        self.assertEqual(self.gate(code=0)["validation"], "PASS")

    def test_edits_and_new_files_invalidate_evidence(self):
        self.setup_task()
        self.run_check()
        self.assess()
        (self.repo / "new.txt").write_text("unfinished work")
        self.assertEqual(self.gate()["missing"], ["test"])
        self.run_check()
        self.assess()
        (self.repo / "source.txt").write_text("changed")
        self.assertEqual(self.gate()["validation"], "INCONCLUSIVE")

    def test_branch_change_invalidates(self):
        self.setup_task()
        self.run_check()
        self.assess()
        self.git("checkout", "-qb", "different")
        self.assertEqual(self.gate()["missing"], ["test"])

    def test_source_mutating_check_cannot_pass(self):
        self.setup_task("from pathlib import Path; Path('source.txt').write_text('mutated')")
        self.assertEqual(self.run_check(code=1)["status"], "INCONCLUSIVE")
        self.assess()
        self.assertNotEqual(self.gate()["validation"], "PASS")

    def test_inherited_failure_is_not_excused(self):
        self.setup_task("raise SystemExit(1)")
        self.assertEqual(self.run_check(code=1)["status"], "FAIL")
        self.assess()
        self.assertEqual(self.gate()["validation"], "FAIL")

    def test_blocked_runner_is_not_pass(self):
        c = self.setup_task()
        # A fresh contract with an unavailable executable is accepted, but launch is BLOCKED.
        c["checks"]["test"]["argv"] = ["/missing/runner"]
        self.contract.write_text(json.dumps(c))
        self.call("start", "--task", "blocked", "--repo", self.repo, "--contract", self.contract)
        e = self.call("run", "--task", "blocked", "--check", "test", code=1)
        self.assertEqual(e["status"], "BLOCKED")

    def test_shared_reservations_cannot_double_spend(self):
        self.setup_task()
        self.call("reserve", "--id", "investigator", "--role", "investigator", "--seconds", "18")
        self.call("reserve", "--id", "reviewer", "--role", "reviewer", "--seconds", "3", code=2)
        self.run_check(code=2)
        self.call("settle", "--id", "investigator", "--seconds", "1")
        self.run_check()
        self.call("settle", "--id", "investigator", "--seconds", "1", code=2)

    def test_helper_count_and_internal_role_are_protected(self):
        self.setup_task(helpers=0)
        self.call("reserve", "--id", "x", "--role", "reviewer", "--seconds", "1", code=2)
        self.call("reserve", "--id", "x", "--role", "validation", "--seconds", "1", code=2)

    def test_attention_budget_and_accounting(self):
        self.setup_task(attention_minutes=5)
        self.call("attention", "--minutes", "5")
        self.run_check(code=2)
        self.call("attention", "--minutes", "-1", code=2)
        self.call("reserve", "--id", "x", "--role", "reviewer", "--seconds", "nan", code=2)

    def test_two_failed_hypotheses_stop_new_work(self):
        self.setup_task()
        for description in ("bad mock", "bad cache"):
            self.call("hypothesis", "--task", "one", "--description", description, "--result", "failed")
        self.run_check(code=2)
        self.call("hypothesis", "--task", "one", "--description", "bad mock", "--result", "failed", code=2)

    def test_out_of_scope_command_denied_and_discovery_parked(self):
        self.setup_task()
        self.call("run", "--task", "one", "--check", "publish", code=2)
        self.call("park", "--task", "one", "--next", "Unrelated improvement")
        self.assertEqual(json.loads(self.state.read_text())["tasks"]["one"]["parked"], ["Unrelated improvement"])

    def test_timeout_kills_owned_process_group(self):
        self.setup_task("import time; time.sleep(10)", timeout=0.1)
        begin = time.monotonic()
        self.assertEqual(self.run_check(code=1)["status"], "BLOCKED")
        self.assertLess(time.monotonic() - begin, 3)
        self.assertTrue(all(x["state"] == "settled" for x in json.loads(self.state.read_text())["reservations"].values()))

    def test_concurrent_call_cannot_spend_same_ledger(self):
        self.setup_task("import time; time.sleep(0.8)", timeout=3)
        p = subprocess.Popen([sys.executable, str(CLI), "--state", str(self.state), "run",
                              "--task", "one", "--check", "test"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        for _ in range(100):
            if json.loads(self.state.read_text())["reservations"]: break
            time.sleep(0.01)
        self.call("reserve", "--id", "x", "--role", "reviewer", "--seconds", "1", code=2)
        stdout, stderr = p.communicate(timeout=5)
        self.assertEqual(p.returncode, 0, stdout + stderr)

    def test_checkpoint_restore_preserves_dirty_work_and_evidence(self):
        self.setup_task()
        (self.repo / "source.txt").write_text("staged")
        self.git("add", "source.txt")
        (self.repo / "source.txt").write_text("unstaged")
        (self.repo / "new.txt").write_text("untracked")
        self.run_check()
        original = self.call("status", "--task", "one")["fingerprint"]
        output = self.root / "checkpoint.tar.gz"
        cp = self.call("checkpoint", "--task", "one", "--output", output, "--next", "Continue fixture")
        self.assertEqual(cp["durability"], "unconfirmed")
        self.run_check(code=2)
        self.call("resume", "--task", "one", "--expect", original, code=2)
        self.call("durable", "--task", "one", "--location", "test persistent destination receipt")
        self.call("resume", "--task", "one", "--expect", original)
        extracted = self.root / "extracted"
        with tarfile.open(output) as tar: tar.extractall(extracted, filter="data")
        restored = self.root / "restored"
        subprocess.check_call(["git", "clone", "-q", str(extracted / "history.bundle"), str(restored)])
        for source in (extracted / "worktree").iterdir():
            (restored / source.name).write_bytes(source.read_bytes())
        self.git("apply", "--cached", str(extracted / "index.patch"), repo=restored)
        self.assertEqual((restored / "source.txt").read_text(), "unstaged")
        self.assertEqual(self.git("show", ":source.txt", repo=restored), b"staged")
        self.assertEqual((restored / "new.txt").read_text(), "untracked")
        self.assertTrue((extracted / "logs").is_dir())

    def test_resume_refuses_mismatched_tree(self):
        self.setup_task()
        fp = self.call("status", "--task", "one")["fingerprint"]
        self.call("checkpoint", "--task", "one", "--output", self.root / "cp.tgz", "--next", "Resume")
        self.call("durable", "--task", "one", "--location", "receipt")
        (self.repo / "source.txt").write_text("different")
        self.call("resume", "--task", "one", "--expect", fp, code=2)

    def test_deadline_extension_preserves_old_usage(self):
        self.setup_task()
        self.run_check()
        s = json.loads(self.state.read_text())
        old_usage = s["used_seconds"]
        s["deadline"] = s["tasks"]["one"]["deadline"] = time.time() - 1
        self.state.write_text(json.dumps(s))
        self.run_check(code=2)
        self.call("extend", "--task", "one", "--minutes", "1", "--owner-request", "Owner approved continuation")
        self.assertEqual(json.loads(self.state.read_text())["used_seconds"], old_usage)
        self.run_check()

    def test_pr_mode_requires_real_external_gates(self):
        c = self.setup_task()
        c["mode"] = "pr"
        self.contract.write_text(json.dumps(c))
        self.call("start", "--task", "pr", "--repo", self.repo, "--contract", self.contract, code=2)
        c["required_external"] = ["repo-ci", "final-adversarial-review"]
        self.contract.write_text(json.dumps(c))
        self.call("start", "--task", "pr", "--repo", self.repo, "--contract", self.contract)
        self.call("run", "--task", "pr", "--check", "test")
        self.call("assess", "--task", "pr", "--outcome", "met", "--evidence", "accepted")
        self.call("receipt", "--task", "pr", "--kind", "repo-ci", "--reference", "fake-green",
                  "--head", self.git("rev-parse", "HEAD").decode().strip(), "--result", "PASS")
        self.assertEqual(self.call("gate", "--task", "pr", code=1)["validation"], "BLOCKED")
        for kind in c["required_external"]:
            self.call("receipt", "--task", "pr", "--kind", kind,
                      "--reference", "https://example.invalid/fixture-evidence", "--verified-by", "test-fixture",
                      "--head", self.git("rev-parse", "HEAD").decode().strip(), "--result", "PASS")
        self.assertEqual(self.call("gate", "--task", "pr")["validation"], "PASS")
        (self.repo / "source.txt").write_text("dirty candidate")
        self.assertEqual(self.call("gate", "--task", "pr", code=1)["validation"], "BLOCKED")

    def test_external_failure_supersedes_green_and_wrong_head_denied(self):
        c = self.setup_task()
        c["required_external"] = ["review"]
        self.contract.write_text(json.dumps(c))
        self.call("start", "--task", "review", "--repo", self.repo, "--contract", self.contract)
        self.call("run", "--task", "review", "--check", "test")
        self.call("assess", "--task", "review", "--outcome", "met", "--evidence", "fixture")
        base = ["receipt", "--task", "review", "--kind", "review", "--reference", "/fixture/evidence",
                "--verified-by", "fixture-reviewer", "--head"]
        self.call(*base, "wrong-sha", "--result", "PASS", code=2)
        head = self.git("rev-parse", "HEAD").decode().strip()
        self.call(*base, head, "--result", "PASS")
        self.assertEqual(self.call("gate", "--task", "review")["validation"], "PASS")
        self.call(*base, head, "--result", "FAIL")
        self.assertEqual(self.call("gate", "--task", "review", code=1)["validation"], "FAIL")


if __name__ == "__main__":
    unittest.main()
