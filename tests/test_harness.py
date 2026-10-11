import json
import os
import signal
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

    def setup_task(self, script="print('ok')", timeout=3):
        self.call("init")
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

    def test_session_has_no_numerical_budget(self):
        self.setup_task()
        s = json.loads(self.state.read_text())
        self.assertEqual(set(s), {"version", "tasks", "events"})
        self.assertEqual(s["version"], 2)
        self.assertNotIn("deadline", s["tasks"]["one"])
        self.run_check()
        self.assess()
        self.assertNotIn("remaining_execution_seconds", self.gate(code=0))
        self.assertEqual(set(json.loads(self.state.read_text())), {"version", "tasks", "events"})

    def test_removed_commands_and_flags_are_rejected_without_writes(self):
        self.setup_task()
        before = self.state.read_bytes()
        removed = [[name] for name in ("reserve", "settle", "attention", "extend")]
        removed += [["init", flag, "1"] for flag in
                    ("--minutes", "--execution-seconds", "--attention-minutes", "--helpers")]
        removed += [["start", "--task", "two", "--repo", str(self.repo),
                     "--contract", str(self.contract), "--minutes", "1"]]
        for args in removed:
            with self.subTest(args=args):
                p = subprocess.run([sys.executable, str(CLI), "--state", str(self.state), *args],
                                   capture_output=True, text=True)
                self.assertEqual(p.returncode, 2)
                self.assertEqual(self.state.read_bytes(), before)

    def test_old_ledgers_are_rejected_without_losing_history(self):
        self.setup_task()
        self.run_check()
        s = json.loads(self.state.read_text())
        s.update(version=1, deadline=0, used_seconds=1, attention_minutes=1,
                 limits={"execution_seconds": 20, "helpers": 1, "attention_minutes": 2},
                 reservations={"check-old": {"state": "held", "role": "validation", "seconds": 3}})
        s["tasks"]["one"]["deadline"] = 0
        self.state.write_text(json.dumps(s))
        before = self.state.read_bytes()
        for args in (("status", "--task", "one"), ("run", "--task", "one", "--check", "test"), ("init",)):
            self.call(*args, code=2)
            self.assertEqual(self.state.read_bytes(), before)

    def test_invalid_command_timeouts_remain_denied(self):
        c = self.setup_task()
        for timeout in (0, -1, True, "3", float("inf"), float("nan")):
            c["checks"]["test"]["timeout_seconds"] = timeout
            self.contract.write_text(json.dumps(c))
            self.call("start", "--task", "invalid", "--repo", self.repo, "--contract", self.contract, code=2)

    def test_success_requires_checks_and_scope(self):
        self.setup_task()
        self.assertEqual(self.gate()["validation"], "INCONCLUSIVE")
        self.run_check()
        self.assertEqual(self.gate()["validation"], "INCONCLUSIVE")
        self.assess()
        self.assertEqual(self.gate(code=0)["validation"], "PASS")

    def test_claude_conflict_cannot_be_cleared_by_acceptance_notes(self):
        c = self.setup_task()
        c["non_goals"] = ["New input validation or features"]
        self.contract.write_text(json.dumps(c))
        self.call("start", "--task", "duration", "--repo", self.repo, "--contract", self.contract)
        self.call("run", "--task", "duration", "--check", "test")
        self.call("assess", "--task", "duration", "--outcome", "met",
                  "--evidence", "Follow-up input validation passes despite the frozen exclusion")
        # Arbitrary prose is not classified. The enforceable boundary starts at declaration.
        self.assertEqual(self.call("gate", "--task", "duration")["validation"], "PASS")
        result = self.call("assess", "--task", "duration", "--outcome", "conflict",
                           "--evidence", "Owner follow-up requires validation excluded by this contract")
        self.assertEqual(result["validation"], "BLOCKED")
        self.call("assess", "--task", "duration", "--outcome", "met",
                  "--evidence", "Put the new requirements in acceptance notes", code=2)
        self.call("assess", "--task", "duration", "--outcome", "unknown", "--evidence", "Reconsidering")
        self.assertEqual(self.call("gate", "--task", "duration", code=1)["validation"], "BLOCKED")
        self.call("run", "--task", "duration", "--check", "test", code=2)

    def test_material_followup_preserves_session_and_needs_fresh_evidence(self):
        c = self.setup_task()
        self.run_check()
        self.assess()
        accepted = json.loads(self.state.read_text())["tasks"]["one"]
        self.call("assess", "--task", "one", "--outcome", "conflict", "--evidence", "Changed acceptance")
        before = json.loads(self.state.read_text())
        self.assertEqual(before["tasks"]["one"]["scope_evidence"], accepted["scope_evidence"])
        c["owner_request"] = "Owner follow-up: validate input as well as formatting"
        c["must_happen"].append("reject invalid input")
        c["checks"]["validation"] = {"argv": [sys.executable, "-c",
            "from pathlib import Path; assert Path('source.txt').read_text() == 'validated\\n'"],
            "timeout_seconds": 3}
        self.contract.write_text(json.dumps(c))
        self.call("start", "--task", "two", "--follows", "one", "--repo", self.repo,
                  "--contract", self.contract)
        after = json.loads(self.state.read_text())
        self.assertEqual(before["version"], after["version"])
        self.assertEqual(after["events"][:len(before["events"])], before["events"])
        old, new = after["tasks"]["one"], after["tasks"]["two"]
        for field in ("contract", "evidence", "external", "contract_conflicts"):
            self.assertEqual(old[field], before["tasks"]["one"][field])
        self.assertEqual(old["superseded_by"], "two")
        self.assertEqual(new["follows"], "one")
        self.assertEqual(new["contract"], c)
        self.assertEqual(new["evidence"], [])
        self.assertEqual(new["external"], [])
        self.call("init", code=2)
        self.assertEqual(self.call("gate", "--task", "one", code=1)["validation"], "BLOCKED")
        self.call("assess", "--task", "two", "--outcome", "met", "--evidence", "New criteria observed")
        self.assertEqual(self.call("gate", "--task", "two", code=1)["missing"], ["test", "validation"])
        self.call("run", "--task", "two", "--check", "validation", code=1)
        self.assertEqual(self.call("gate", "--task", "two", code=1)["validation"], "FAIL")
        (self.repo / "source.txt").write_text("validated\n")
        self.call("run", "--task", "two", "--check", "test")
        self.call("run", "--task", "two", "--check", "validation")
        self.call("assess", "--task", "two", "--outcome", "met", "--evidence", "New criterion observed on changed tree")
        self.assertEqual(self.call("gate", "--task", "two")["validation"], "PASS")
        # Editing the input contract cannot rewrite either frozen ledger contract.
        self.contract.write_text("{}")
        self.assertEqual(json.loads(self.state.read_text())["tasks"]["two"]["contract"], c)

    def test_clarification_keeps_current_contract_and_task(self):
        self.setup_task()
        self.run_check()
        self.assess()
        before = json.loads(self.state.read_text())
        self.call("assess", "--task", "one", "--outcome", "met",
                  "--evidence", "Clarification: same acceptance examples, observed again")
        self.assertEqual(self.gate(code=0)["validation"], "PASS")
        after = json.loads(self.state.read_text())
        self.assertEqual(list(after["tasks"]), ["one"])
        self.assertEqual(after["tasks"]["one"]["contract"], before["tasks"]["one"]["contract"])

    def test_followup_requires_valid_predecessor_and_authorization_reference(self):
        c = self.setup_task()
        before = self.state.read_bytes()
        for predecessor in ("missing", "one"):
            self.call("start", "--task", "two", "--follows", predecessor, "--repo", self.repo,
                      "--contract", self.contract, code=2)
            self.assertEqual(self.state.read_bytes(), before)
        c["owner_request"] = "Owner follow-up"
        self.contract.write_text(json.dumps(c))
        self.call("start", "--task", "two", "--follows", "one", "--repo", self.repo, "--contract", self.contract)
        self.call("start", "--task", "three", "--follows", "one", "--repo", self.repo,
                  "--contract", self.contract, code=2)

    def test_pr_followup_retains_obligations_and_cannot_reuse_receipts(self):
        c = self.setup_task()
        c.update(mode="pr", required_external=["repo-ci", "final-adversarial-review", "security"])
        self.contract.write_text(json.dumps(c))
        self.call("start", "--task", "pr", "--repo", self.repo, "--contract", self.contract)
        self.call("run", "--task", "pr", "--check", "test")
        self.call("assess", "--task", "pr", "--outcome", "met", "--evidence", "Observed")
        head = self.git("rev-parse", "HEAD").decode().strip()
        for kind in c["required_external"]:
            self.call("receipt", "--task", "pr", "--kind", kind, "--reference", "/fixture/evidence",
                      "--verified-by", "fixture", "--head", head, "--result", "PASS")
        self.assertEqual(self.call("gate", "--task", "pr")["validation"], "PASS")
        c["owner_request"] = "Owner follow-up"
        for mode, external in (("local", []), ("pr", ["repo-ci", "final-adversarial-review"])):
            bad = dict(c, mode=mode, required_external=external)
            self.contract.write_text(json.dumps(bad))
            self.call("start", "--task", "next", "--follows", "pr", "--repo", self.repo,
                      "--contract", self.contract, code=2)
        self.contract.write_text(json.dumps(c))
        self.call("start", "--task", "next", "--follows", "pr", "--repo", self.repo, "--contract", self.contract)
        self.call("run", "--task", "next", "--check", "test")
        self.call("assess", "--task", "next", "--outcome", "met", "--evidence", "Observed follow-up")
        self.assertEqual(self.call("gate", "--task", "next", code=1)["validation"], "BLOCKED")

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

    def test_two_failed_hypotheses_stop_new_work(self):
        self.setup_task()
        for description in ("bad mock", "bad cache"):
            self.call("hypothesis", "--task", "one", "--description", description, "--result", "failed")
        self.run_check(code=2)
        self.call("hypothesis", "--task", "one", "--description", "bad mock", "--result", "failed", code=2)
        history = json.loads(self.state.read_text())["tasks"]["one"]["hypotheses"]
        self.call("diagnose", "--task", "one", "--new-evidence", "New fixture cause", code=2)
        fp = self.call("status", "--task", "one")["fingerprint"]
        self.call("checkpoint", "--task", "one", "--output", self.root / "diagnosis.tgz", "--next", "Inspect cause")
        self.call("durable", "--task", "one", "--location", "fixture receipt")
        self.call("resume", "--task", "one", "--expect", fp, code=2)
        self.call("diagnose", "--task", "one", "--new-evidence", "New fixture cause")
        self.call("diagnose", "--task", "one", "--new-evidence", "New fixture cause", code=2)
        self.call("resume", "--task", "one", "--expect", fp)
        self.run_check()
        self.assertEqual(json.loads(self.state.read_text())["tasks"]["one"]["hypotheses"], history)

    def test_out_of_scope_command_denied_and_discovery_parked(self):
        self.setup_task()
        self.call("run", "--task", "one", "--check", "publish", code=2)
        self.call("park", "--task", "one", "--next", "Unrelated improvement")
        self.assertEqual(json.loads(self.state.read_text())["tasks"]["one"]["parked"], ["Unrelated improvement"])

    def test_timeout_kills_owned_process_group(self):
        child_pid = self.root / "child.pid"
        self.setup_task("import subprocess, sys, time; from pathlib import Path; "
                        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']); "
                        f"Path({str(child_pid)!r}).write_text(str(child.pid)); time.sleep(10)", timeout=0.5)
        begin = time.monotonic()
        self.assertEqual(self.run_check(code=1)["status"], "BLOCKED")
        self.assertLess(time.monotonic() - begin, 3)
        pid = int(child_pid.read_text())
        for _ in range(100):
            child_state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                                         text=True).stdout.strip()
            if not child_state or child_state.startswith("Z"): break
            time.sleep(0.01)
        self.assertTrue(not child_state or child_state.startswith("Z"), child_state)
        self.assertEqual(self.gate()["validation"], "BLOCKED")

    def test_killed_lead_cannot_reveal_previous_pass(self):
        marker = self.root / "slow"
        self.setup_task(f"from pathlib import Path; import time; "
                        f"time.sleep(10) if Path({str(marker)!r}).exists() else None", timeout=15)
        self.run_check()
        self.assess()
        self.assertEqual(self.gate(code=0)["validation"], "PASS")
        marker.touch()  # External fixture control leaves the repository fingerprint unchanged.
        p = subprocess.Popen([sys.executable, str(CLI), "--state", str(self.state), "run",
                              "--task", "one", "--check", "test"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        group = None
        try:
            for _ in range(200):
                evidence = json.loads(self.state.read_text())["tasks"]["one"]["evidence"]
                if len(evidence) == 2 and evidence[-1].get("process_group"):
                    group = evidence[-1]["process_group"]
                    break
                time.sleep(0.01)
            self.assertIsNotNone(group)
            p.kill()
            p.communicate(timeout=3)
            result = self.gate()
            self.assertEqual(result["validation"], "BLOCKED")
            self.assertIn("test", result["blocked"])
        finally:
            if p.poll() is None:
                p.kill()
                p.communicate(timeout=3)
            if group is not None:
                try: os.killpg(group, signal.SIGKILL)
                except ProcessLookupError: pass

    def test_interrupt_terminates_check_and_records_uncertainty(self):
        self.setup_task("import time; time.sleep(10)", timeout=15)
        p = subprocess.Popen([sys.executable, str(CLI), "--state", str(self.state), "run",
                              "--task", "one", "--check", "test"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        group = None
        try:
            for _ in range(200):
                evidence = json.loads(self.state.read_text())["tasks"]["one"]["evidence"]
                if evidence and evidence[-1].get("process_group"):
                    group = evidence[-1]["process_group"]
                    break
                time.sleep(0.01)
            self.assertIsNotNone(group)
            p.send_signal(signal.SIGINT)
            stdout, stderr = p.communicate(timeout=3)
            self.assertEqual(p.returncode, 1, stdout + stderr)
            result = json.loads(stdout)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("interrupted", result["detail"])
            with self.assertRaises(ProcessLookupError): os.kill(group, 0)
            self.assess()
            self.assertEqual(self.gate()["validation"], "BLOCKED")
        finally:
            if p.poll() is None:
                p.kill()
                p.communicate(timeout=3)
            if group is not None:
                try: os.killpg(group, signal.SIGKILL)
                except ProcessLookupError: pass

    def test_concurrent_call_cannot_overwrite_same_ledger(self):
        self.setup_task("import time; time.sleep(0.8)", timeout=3)
        p = subprocess.Popen([sys.executable, str(CLI), "--state", str(self.state), "run",
                              "--task", "one", "--check", "test"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        for _ in range(100):
            evidence = json.loads(self.state.read_text())["tasks"]["one"]["evidence"]
            if evidence and evidence[-1].get("process_group"): break
            time.sleep(0.01)
        during = self.state.read_bytes()
        self.assertIn("busy", self.call("assess", "--task", "one", "--outcome", "met",
                                        "--evidence", "Competing write", code=2)["reason"])
        self.assertEqual(self.state.read_bytes(), during)
        stdout, stderr = p.communicate(timeout=5)
        self.assertEqual(p.returncode, 0, stdout + stderr)
        s = json.loads(self.state.read_text())
        self.assertEqual(len(s["tasks"]["one"]["evidence"]), 1)
        self.assertEqual(s["tasks"]["one"]["scope_outcome"], "unknown")

    def test_checkpoint_restore_preserves_dirty_work_and_evidence(self):
        self.setup_task()
        (self.repo / "source.txt").write_text("staged")
        self.git("add", "source.txt")
        (self.repo / "source.txt").write_text("unstaged")
        (self.repo / "new.txt").write_text("untracked")
        self.run_check()
        self.assess()
        original = self.call("status", "--task", "one")["fingerprint"]
        output = self.root / "checkpoint.tar.gz"
        cp = self.call("checkpoint", "--task", "one", "--output", output, "--next", "Continue fixture")
        self.assertEqual(cp["durability"], "unconfirmed")
        self.run_check(code=2)
        self.call("resume", "--task", "one", "--expect", original, code=2)
        self.call("durable", "--task", "one", "--location", "test persistent destination receipt")
        self.call("resume", "--task", "one", "--expect", original)
        extracted = self.root / "extracted"
        # This archive was created from this test's trusted temporary fixture.
        # Python 3.10 before 3.10.12 has no extraction-filter API.
        with tarfile.open(output) as tar:
            options = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tar.extractall(extracted, **options)
        restored = self.root / "restored"
        subprocess.check_call(["git", "clone", "-q", str(extracted / "history.bundle"), str(restored)])
        for source in (extracted / "worktree").iterdir():
            (restored / source.name).write_bytes(source.read_bytes())
        self.git("apply", "--cached", str(extracted / "index.patch"), repo=restored)
        self.assertEqual((restored / "source.txt").read_text(), "unstaged")
        self.assertEqual(self.git("show", ":source.txt", repo=restored), b"staged")
        self.assertEqual((restored / "new.txt").read_text(), "untracked")
        self.assertTrue((extracted / "logs").is_dir())
        recovered = json.loads((extracted / "state.json").read_text())
        task = recovered["tasks"]["one"]
        task["repo"] = str(restored)
        task["checkpoint"]["sha256"] = cp["sha256"]
        for e in task["evidence"]:
            e["log"] = str(extracted / "logs" / Path(e["log"]).name)
        self.state = extracted / "state.json"
        self.state.write_text(json.dumps(recovered))
        self.call("durable", "--task", "one", "--location", "recovered fixture receipt")
        self.call("resume", "--task", "one", "--expect", original)
        self.assertEqual(self.gate(code=0)["validation"], "PASS")
        self.assertEqual(json.loads(self.state.read_text())["tasks"]["one"]["contract"], task["contract"])

    def test_resume_refuses_mismatched_tree(self):
        self.setup_task()
        fp = self.call("status", "--task", "one")["fingerprint"]
        self.call("checkpoint", "--task", "one", "--output", self.root / "cp.tgz", "--next", "Resume")
        self.call("durable", "--task", "one", "--location", "receipt")
        (self.repo / "source.txt").write_text("different")
        self.call("resume", "--task", "one", "--expect", fp, code=2)

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
