import json
import copy
import contextlib
import hashlib
import importlib.util
import io
import os
import re
import signal
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

CLI = Path(__file__).resolve().parents[1] / "lean.py"
SPEC = importlib.util.spec_from_file_location("lean_harness", CLI)
HARNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HARNESS)


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

    def assert_latest_attempt_prevents_revival(self, script, expected_status, timeout=3):
        marker = self.root / "later-attempt"
        self.setup_task("from pathlib import Path; import time; "
                        f"later = Path({str(marker)!r}).exists(); " + script, timeout=timeout)
        self.run_check()
        self.assess()
        self.assertEqual(self.gate(code=0)["validation"], "PASS")
        (self.repo / "source.txt").write_text("changed\n")
        marker.touch()
        self.assertEqual(self.run_check(code=1)["status"], expected_status)
        if expected_status == "INCONCLUSIVE":
            self.assertEqual((self.repo / "source.txt").read_text(), "original\n")
        (self.repo / "source.txt").write_text("original\n")
        recorded = json.loads(self.state.read_text())
        result = self.gate()
        self.assertNotEqual(result["validation"], "PASS")
        self.assertEqual(result["missing"], ["test"])
        # Existing format-2 history is interpreted directly, without migration or rewriting evidence.
        observed = json.loads(self.state.read_text())
        self.assertEqual(observed["version"], 2)
        self.assertEqual(observed["tasks"]["one"]["evidence"], recorded["tasks"]["one"]["evidence"])
        marker.unlink()
        self.run_check()
        self.assertEqual(self.gate(code=0)["validation"], "PASS")

    def test_inconclusive_attempt_cannot_revive_older_pass_after_restoring_source(self):
        self.assert_latest_attempt_prevents_revival(
            "Path('source.txt').write_text('original\\n') if later else None", "INCONCLUSIVE")

    def test_failed_attempt_cannot_revive_older_pass_after_restoring_source(self):
        self.assert_latest_attempt_prevents_revival("raise SystemExit(1 if later else 0)", "FAIL")

    def test_blocked_attempt_cannot_revive_older_pass_after_restoring_source(self):
        self.assert_latest_attempt_prevents_revival("time.sleep(10) if later else None", "BLOCKED", timeout=0.5)

    def test_refreshing_one_check_cannot_revive_another_checks_older_pass(self):
        c = self.setup_task()
        c["checks"]["other"] = dict(c["checks"]["test"])
        self.contract.write_text(json.dumps(c))
        self.call("start", "--task", "two-checks", "--repo", self.repo, "--contract", self.contract)
        for name in ("test", "other"):
            self.call("run", "--task", "two-checks", "--check", name)
        self.call("assess", "--task", "two-checks", "--outcome", "met", "--evidence", "Both checks observed")
        self.assertEqual(self.call("gate", "--task", "two-checks")["validation"], "PASS")
        (self.repo / "source.txt").write_text("changed\n")
        for name in ("test", "other"):
            self.call("run", "--task", "two-checks", "--check", name)
        (self.repo / "source.txt").write_text("original\n")
        self.call("run", "--task", "two-checks", "--check", "test")
        self.assertEqual(self.call("gate", "--task", "two-checks", code=1)["missing"], ["other"])
        self.call("run", "--task", "two-checks", "--check", "other")
        self.assertEqual(self.call("gate", "--task", "two-checks")["validation"], "PASS")

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
        self.assert_killed_lead_cannot_reveal_previous_pass(restore_source=False)

    def test_killed_lead_on_changed_tree_cannot_revive_older_pass(self):
        self.assert_killed_lead_cannot_reveal_previous_pass(restore_source=True)

    def assert_killed_lead_cannot_reveal_previous_pass(self, restore_source):
        marker = self.root / "slow"
        self.setup_task(f"from pathlib import Path; import time; "
                        f"time.sleep(10) if Path({str(marker)!r}).exists() else None", timeout=15)
        self.run_check()
        self.assess()
        self.assertEqual(self.gate(code=0)["validation"], "PASS")
        marker.touch()  # External control chooses a slow check without mutating its source.
        if restore_source:
            (self.repo / "source.txt").write_text("changed\n")
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
            self.assertEqual(json.loads(self.state.read_text())["tasks"]["one"]["evidence"][-1]["status"], "BLOCKED")
            if restore_source:
                (self.repo / "source.txt").write_text("original\n")
            result = self.gate()
            if restore_source:
                self.assertEqual(result["validation"], "INCONCLUSIVE")
                self.assertIn("test", result["missing"])
            else:
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

    def test_linked_checkpoint_preserves_whole_session_logs(self):
        c = self.setup_task()
        self.run_check()
        c["owner_request"] = "authorized linked fixture follow-up"
        self.contract.write_text(json.dumps(c))
        self.call("start", "--task", "two", "--follows", "one", "--repo", self.repo,
                  "--contract", self.contract)
        self.call("run", "--task", "two", "--check", "test")
        output = self.root / "linked.tgz"
        self.call("checkpoint", "--task", "two", "--output", output, "--next", "Continue")
        with tarfile.open(output) as tar:
            embedded = json.load(tar.extractfile("state.json"))
            for task in embedded["tasks"].values():
                for evidence in task["evidence"]:
                    member = "logs/" + Path(evidence["log"]).name
                    self.assertIn(member, tar.getnames())
                    self.assertEqual(tar.extractfile(member).read(), Path(evidence["log"]).read_bytes())

    def assert_manifest(self, output, session):
        with tarfile.open(output) as tar:
            manifest = json.load(tar.extractfile("log-manifest.json"))
            expected = {(task_id, i) for task_id, task in session["tasks"].items()
                        for i, _ in enumerate(task["evidence"])}
            self.assertEqual({(r["task"], r["evidence_index"]) for r in manifest["references"]}, expected)
            for ref in manifest["references"]:
                if ref["availability"] == "available":
                    self.assertEqual(hashlib.sha256(tar.extractfile(ref["member"]).read()).hexdigest(),
                                     ref["sha256"])
            payloads = [m.name for m in tar.getmembers() if m.isfile() and m.name.startswith("logs/")]
            self.assertEqual(len(payloads), len(set(payloads)))
            return manifest, payloads

    def test_checkpoint_manifest_deduplicates_aliases_without_basename_collisions(self):
        self.setup_task()
        self.run_check()
        session = json.loads(self.state.read_text())
        task = session["tasks"]["one"]
        source = Path(task["evidence"][0]["log"])
        for folder, contents in (("alias", None), ("collision", b"distinct log bytes")):
            other = self.root / folder / source.name
            other.parent.mkdir()
            if contents is None:
                os.link(source, other)
            else:
                other.write_bytes(contents)
            record = copy.deepcopy(task["evidence"][0])
            record["log"] = str(other)
            task["evidence"].append(record)
        # The same literal path and a distinct hardlink are both single-payload references.
        task["evidence"].append(copy.deepcopy(task["evidence"][0]))
        output = self.root / "aliases.tgz"
        HARNESS.checkpoint(session, task, output, "Continue")
        manifest, payloads = self.assert_manifest(output, session)
        refs = manifest["references"]
        self.assertEqual(len(payloads), 2)
        self.assertEqual(refs[0]["member"], refs[1]["member"])
        self.assertEqual(refs[0]["member"], refs[3]["member"])
        self.assertNotEqual(refs[0]["member"], refs[2]["member"])

    def test_checkpoint_other_repository_logs_but_only_selected_checkout(self):
        self.setup_task()
        self.run_check()
        other = self.root / "other"
        other.mkdir()
        for args in (["init", "-q"], ["config", "user.email", "test@example.invalid"],
                     ["config", "user.name", "Harness test"]):
            self.git(*args, repo=other)
        (other / "other-only.txt").write_text("Other repository")
        self.git("add", ".", repo=other)
        self.git("commit", "-qm", "Other fixture", repo=other)
        self.call("start", "--task", "other", "--repo", other, "--contract", self.contract)
        self.call("run", "--task", "other", "--check", "test")
        output = self.root / "other.tgz"
        self.call("checkpoint", "--task", "one", "--output", output, "--next", "Continue")
        self.assert_manifest(output, json.loads(self.state.read_text()))
        with tarfile.open(output) as tar:
            self.assertIn("worktree/source.txt", tar.getnames())
            self.assertNotIn("worktree/other-only.txt", tar.getnames())

    def test_checkpoint_missing_completed_logs_preserves_disk_and_memory(self):
        for completed in ("PASS", "FAIL", "INCONCLUSIVE", "BLOCKED"):
            with self.subTest(completed=completed):
                if not self.state.exists():
                    self.setup_task()
                    self.run_check()
                session = json.loads(self.state.read_text())
                task = session["tasks"]["one"]
                task["evidence"][0]["status"] = completed
                task["evidence"][0]["exit_code"] = None
                Path(task["evidence"][0]["log"]).unlink(missing_ok=True)
                before = copy.deepcopy(session)
                disk = self.state.read_bytes()
                output = self.root / (completed + ".tgz")
                with self.assertRaisesRegex(HARNESS.Denied, "Completed check log missing"):
                    HARNESS.checkpoint(session, task, output, "Continue")
                self.assertEqual(session, before)
                self.assertEqual(self.state.read_bytes(), disk)
                self.assertFalse(output.exists())
                # Exercise the actual CLI's same no-write boundary as well.
                self.call("checkpoint", "--task", "one", "--output", output, "--next", "Continue", code=2)
                self.assertEqual(self.state.read_bytes(), disk)

    def test_checkpoint_unfinished_log_histories_are_honestly_accounted(self):
        self.setup_task()
        self.run_check()
        original = json.loads(self.state.read_text())
        for history in ("before-open", "after-open", "deleted-later", "legacy-before-open"):
            with self.subTest(history=history):
                session = copy.deepcopy(original)
                task = session["tasks"]["one"]
                evidence = task["evidence"][0]
                evidence.update(status="BLOCKED", exit_code=None,
                                detail="Check started without a recorded completion; verify owned process before rerunning")
                session["events"] = [e for e in session["events"] if e["event"] != "check-finish"]
                log = Path(evidence["log"])
                if history == "legacy-before-open":
                    evidence.pop("attempt_id", None)
                if history == "after-open":
                    log.write_bytes(b"")
                elif history == "deleted-later":
                    log.write_bytes(b"partial output")
                    log.unlink()
                else:
                    log.unlink(missing_ok=True)
                output = self.root / (history + ".tgz")
                HARNESS.checkpoint(session, task, output, "Inspect owned process")
                manifest, _ = self.assert_manifest(output, session)
                ref = manifest["references"][0]
                if history == "after-open":
                    self.assertEqual(ref["availability"], "available")
                    self.assertEqual(ref["sha256"], hashlib.sha256(b"").hexdigest())
                else:
                    self.assertEqual(ref["availability"], "unavailable")
                    self.assertEqual(ref["creation_history"], "unknown")
                    self.assertNotIn("member", ref)

    def test_checkpoint_hashes_captured_log_bytes_despite_source_log_movement(self):
        self.setup_task()
        self.run_check()
        session = json.loads(self.state.read_text())
        task = session["tasks"]["one"]
        log = Path(task["evidence"][0]["log"])
        original = log.read_bytes()
        copy_log = HARNESS.shutil.copyfileobj
        def moving_log(source, destination, *args, **kwargs):
            log.rename(log.with_suffix(".moved"))
            log.write_bytes(b"replacement before archive publication")
            return copy_log(source, destination, *args, **kwargs)
        output = self.root / "moving-log.tgz"
        with mock.patch.object(HARNESS.shutil, "copyfileobj", side_effect=moving_log):
            HARNESS.checkpoint(session, task, output, "Continue")
        manifest, _ = self.assert_manifest(output, session)
        with tarfile.open(output) as tar:
            self.assertEqual(tar.extractfile(manifest["references"][0]["member"]).read(), original)
        self.assertNotEqual(log.read_bytes(), original)

    def test_checkpoint_source_movement_and_archive_replace_failure_are_atomic(self):
        self.setup_task()
        self.run_check()
        for failure in ("source-movement", "archive-replace"):
            with self.subTest(failure=failure):
                session = json.loads(self.state.read_text())
                before = copy.deepcopy(session)
                disk = self.state.read_bytes()
                output = self.root / (failure + ".tgz")
                add = HARNESS.tarfile.TarFile.add
                def moving_source(tar, name, *args, **kwargs):
                    result = add(tar, name, *args, **kwargs)
                    if kwargs.get("arcname") == "worktree/source.txt":
                        (self.repo / "source.txt").write_text("changed while archiving")
                    return result
                patch = (mock.patch.object(HARNESS.tarfile.TarFile, "add", new=moving_source)
                         if failure == "source-movement" else
                         mock.patch.object(HARNESS.os, "replace", side_effect=OSError("replace fault")))
                with patch, self.assertRaises((HARNESS.Denied, OSError)):
                    HARNESS.checkpoint(session, session["tasks"]["one"], output, "Continue")
                self.assertEqual(session, before)
                self.assertEqual(self.state.read_bytes(), disk)
                self.assertFalse(output.exists())
                self.assertEqual(list(self.root.glob(".checkpoint-*")), [])

    def test_checkpoint_hash_failure_reports_published_archive_preserves_memory(self):
        self.setup_task()
        self.run_check()
        session = json.loads(self.state.read_text())
        before = copy.deepcopy(session)
        disk = self.state.read_bytes()
        output = self.root / "hash-fault.tgz"
        digest = HARNESS.file_sha256
        def failing_hash(path):
            if Path(path).resolve() == output.resolve():
                raise OSError("hash fault")
            return digest(path)
        with mock.patch.object(HARNESS, "file_sha256", side_effect=failing_hash):
            with self.assertRaises(HARNESS.CheckpointPublished) as raised:
                HARNESS.checkpoint(session, session["tasks"]["one"], output, "Continue")
        self.assertEqual(raised.exception.archive, str(output.resolve()))
        self.assertIsNone(raised.exception.sha256)
        self.assertTrue(output.exists())
        self.assert_manifest(output, session)
        self.assertEqual(session, before)
        self.assertEqual(self.state.read_bytes(), disk)

    def test_checkpoint_ledger_save_failure_reports_archive_identity(self):
        self.setup_task()
        self.run_check()
        disk = self.state.read_bytes()
        output = self.root / "save-fault.tgz"
        stderr = io.StringIO()
        with mock.patch.object(HARNESS, "save", side_effect=OSError("ledger save fault")):
            with contextlib.redirect_stderr(stderr):
                result = HARNESS.main(["--state", str(self.state), "checkpoint", "--task", "one",
                                       "--output", str(output), "--next", "Continue"])
        self.assertEqual(result, 2)
        error = json.loads(stderr.getvalue())
        self.assertEqual(error["archive"], str(output.resolve()))
        self.assertEqual(error["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertEqual(error["ledger_publication"], "unconfirmed")
        self.assertEqual(self.state.read_bytes(), disk)
        self.call("checkpoint", "--task", "one", "--output", output, "--next", "Continue", code=2)
        self.assertEqual(self.state.read_bytes(), disk)

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
        restored = self.root / "restored"
        self.run_generated_recovery(output, restored)
        self.assertEqual((restored / "repo/source.txt").read_text(), "unstaged")
        self.assertEqual(self.git("show", ":source.txt", repo=restored / "repo"), b"staged")
        self.assertEqual((restored / "repo/new.txt").read_text(), "untracked")
        self.state = restored / "state.json"
        recovered = json.loads(self.state.read_text())
        task = recovered["tasks"]["one"]
        self.assertEqual(task["checkpoint"]["sha256"], cp["sha256"])
        self.call("durable", "--task", "one", "--location", "recovered fixture receipt")
        self.call("resume", "--task", "one", "--expect", original)
        self.assertEqual(self.gate(code=0)["validation"], "PASS")
        self.assertEqual(json.loads(self.state.read_text())["tasks"]["one"]["contract"], task["contract"])

    def run_generated_recovery(self, archive, destination, relocated=None, code=0):
        with tarfile.open(archive) as tar:
            resume = tar.extractfile("resume.md").read().decode()
        blocks = re.findall(r"```sh\n(.*?)```", resume, re.S)
        self.assertEqual(len(blocks), 1, "Recovery needs one executable shell block")
        result = subprocess.run(["sh", "-c", blocks[0], "resume", str(destination),
                                 *([str(relocated)] if relocated else [])],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout or result.stderr)

    def test_generated_recovery_preserves_named_branch_full_identity_and_session(self):
        self.git("checkout", "-qb", "repair/nondefault")
        for name in ("deleted.txt", "staged-deleted.txt", "executable"):
            (self.repo / name).write_text("base")
        self.git("add", ".")
        self.git("commit", "-qm", "Recovery base")
        c = self.setup_task()
        self.run_check()
        c["owner_request"] = "linked recovery authorization"
        self.contract.write_text(json.dumps(c))
        self.call("start", "--task", "two", "--follows", "one", "--repo", self.repo,
                  "--contract", self.contract)
        (self.repo / "source.txt").write_text("staged")
        (self.repo / "executable").chmod(0o755)
        self.git("add", "source.txt", "executable")
        (self.repo / "source.txt").write_text("unstaged")
        (self.repo / "deleted.txt").unlink()
        self.git("rm", "--quiet", "staged-deleted.txt")
        nested = self.repo / "nested space"
        nested.mkdir()
        (nested / "new.txt").write_bytes(b"untracked\x00bytes")
        (nested / "link").symlink_to("../source.txt")
        os.link(self.repo / "source.txt", nested / "hardlink")
        self.call("run", "--task", "two", "--check", "test")
        self.call("assess", "--task", "two", "--outcome", "met", "--evidence", "Fixture accepted")
        output = self.root / "source checkpoint.tgz"
        receipt = self.call("checkpoint", "--task", "two", "--output", output, "--next", "Continue")
        before = json.loads(self.state.read_text())
        logs = {(tid, i): Path(e["log"]).read_bytes() for tid, t in before["tasks"].items()
                for i, e in enumerate(t["evidence"])}
        with tarfile.open(output) as tar:
            block = re.findall(r"```sh\n(.*?)```", tar.extractfile("resume.md").read().decode(), re.S)[0]
            archived_events = json.load(tar.extractfile("state.json"))["events"]
        relocated = self.root / "moved ' $ checkpoint.tgz"
        output.rename(relocated)
        HARNESS.shutil.rmtree(self.repo)
        self.state.unlink()
        HARNESS.shutil.rmtree(self.root / "session-logs")
        restored = self.root / "new recovery"
        result = subprocess.run(["sh", "-c", block, "resume", str(restored), str(relocated)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.state = restored / "state.json"
        recovered = json.loads(self.state.read_text())
        self.assertEqual(HARNESS.snapshot(restored / "repo")["fingerprint"],
                         before["tasks"]["two"]["checkpoint_snapshot"]["fingerprint"])
        self.assertEqual(self.git("branch", "--show-current", repo=restored / "repo").decode().strip(),
                         "repair/nondefault")
        self.assertEqual((restored / "repo/executable").stat().st_mode & 0o777, 0o755)
        self.assertEqual(os.readlink(restored / "repo/nested space/link"), "../source.txt")
        self.assertFalse((restored / "repo/deleted.txt").exists())
        self.assertFalse((restored / "repo/staged-deleted.txt").exists())
        self.assertEqual((restored / "repo/nested space/new.txt").read_bytes(), b"untracked\x00bytes")
        for tid, task in recovered["tasks"].items():
            expected = copy.deepcopy(before["tasks"][tid])
            for i, evidence in enumerate(task["evidence"]):
                self.assertEqual(Path(evidence["log"]).read_bytes(), logs[tid, i])
                expected["evidence"][i]["log"] = evidence["log"]
            if tid == "two":
                expected["repo"] = str((restored / "repo").resolve())
                expected["checkpoint"]["path"] = str((restored / "checkpoint.tar.gz").resolve())
            self.assertEqual(task, expected)
        self.assertEqual(recovered["events"], archived_events)
        self.assertEqual(HARNESS.file_sha256(relocated), receipt["sha256"])
        self.assertEqual(HARNESS.file_sha256(restored / "checkpoint.tar.gz"), receipt["sha256"])
        self.call("durable", "--task", "two", "--location", "Authorized fixture storage receipt")
        self.call("resume", "--task", "two", "--expect", json.loads(result.stdout)["fingerprint"])
        self.assertEqual(self.call("gate", "--task", "two")["validation"], "PASS")

    def test_generated_recovery_preserves_unreferenced_detached_head(self):
        self.git("checkout", "--quiet", "--detach")
        (self.repo / "source.txt").write_text("detached-only commit")
        self.git("commit", "-qam", "Detached commit")
        self.assertEqual(self.git("for-each-ref", "--contains", "HEAD"), b"")
        self.setup_task()
        output = self.root / "detached.tgz"
        self.call("checkpoint", "--task", "one", "--output", output, "--next", "Continue")
        restored = self.root / "detached recovery"
        self.run_generated_recovery(output, restored)
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", repo=restored / "repo"), b"HEAD\n")
        self.assertEqual(HARNESS.snapshot(restored / "repo")["fingerprint"], HARNESS.snapshot(self.repo)["fingerprint"])

    def rewritten_archive(self, source, destination, omit=(), extra=()):
        with tarfile.open(source) as original, tarfile.open(destination, "w:gz") as rewritten:
            for member in original.getmembers():
                if member.name not in omit:
                    rewritten.addfile(member, original.extractfile(member) if member.isfile() else None)
            for member, data in extra:
                rewritten.addfile(member, io.BytesIO(data) if data is not None else None)
        return destination

    def test_recovery_rejects_existing_destination_special_and_missing_members(self):
        self.setup_task()
        self.run_check()
        output = self.root / "valid.tgz"
        self.call("checkpoint", "--task", "one", "--output", output, "--next", "Continue")
        helper = CLI.with_name("recover.py")
        existing = self.root / "existing"
        existing.mkdir()
        (existing / "owned.txt").write_text("untouched")
        result = self.run_generated_recovery(output, existing, code=2)
        self.assertIn("already exists", result["reason"])
        self.assertEqual((existing / "owned.txt").read_text(), "untouched")
        dangling = self.root / "dangling"
        dangling.symlink_to(self.root / "absent")
        self.assertIn("already exists", self.run_generated_recovery(output, dangling, code=2)["reason"])
        special = tarfile.TarInfo("worktree/fifo")
        special.type = tarfile.FIFOTYPE
        escape = tarfile.TarInfo("../escaped")
        escape.size = 4
        linked_parent = tarfile.TarInfo("worktree/nested")
        linked_parent.type = tarfile.SYMTYPE
        linked_parent.linkname = "../outside"
        nested_child = tarfile.TarInfo("worktree/nested/file")
        nested_child.size = 4
        cases = [("missing-bundle", ("history.bundle",), (), "Missing required"),
                 ("missing-source", ("worktree/source.txt",), (), "fingerprint differs"),
                 ("special", (), ((special, None),), "special archive member"),
                 ("escape", (), ((escape, b"oops"),), "Unsafe archive member"),
                 ("linked-parent", (), ((linked_parent, None), (nested_child, b"oops")),
                  "Nondirectory archive parent")]
        with tarfile.open(output) as tar:
            log = json.load(tar.extractfile("log-manifest.json"))["references"][0]["member"]
        cases.append(("missing-log", (log,), (), "Missing regular log member"))
        for name, omit, extra, reason in cases:
            with self.subTest(name=name):
                modified = self.rewritten_archive(output, self.root / (name + ".tgz"), omit, extra)
                dest = self.root / (name + "-recovery")
                process = subprocess.run([sys.executable, str(helper), str(modified), str(dest), "--task", "one"],
                                         capture_output=True, text=True)
                self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
                self.assertIn(reason, json.loads(process.stderr)["reason"])
                self.assertFalse(dest.exists())

    def test_companion_recovers_old_archive_without_rewriting_it(self):
        self.git("checkout", "-qb", "legacy/named")
        self.setup_task()
        self.run_check()
        output = self.root / "new.tgz"
        self.call("checkpoint", "--task", "one", "--output", output, "--next", "Continue")
        old_resume = tarfile.TarInfo("resume.md")
        old_note = b"Old clone/checkout-head/overlay instructions\n"
        old_resume.size = len(old_note)
        legacy = self.rewritten_archive(output, self.root / "old.tgz",
                                        ("log-manifest.json", "resume.md"), ((old_resume, old_note),))
        before = legacy.read_bytes()
        dest = self.root / "legacy-recovery"
        process = subprocess.run([sys.executable, str(CLI.with_name("recover.py")),
                                  str(legacy), str(dest), "--task", "one"], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(legacy.read_bytes(), before)
        self.assertEqual((dest / "checkpoint.tar.gz").read_bytes(), before)
        self.assertEqual((dest / "archived-resume.md").read_bytes(), old_note)
        self.assertEqual(HARNESS.snapshot(dest / "repo")["fingerprint"], HARNESS.snapshot(self.repo)["fingerprint"])
        with tarfile.open(legacy) as tar:
            missing = next(m.name for m in tar.getmembers() if m.isfile() and m.name.startswith("logs/"))
        incomplete = self.rewritten_archive(legacy, self.root / "old-missing.tgz", (missing,))
        failed = subprocess.run([sys.executable, str(CLI.with_name("recover.py")),
                                 str(incomplete), str(self.root / "old-missing"), "--task", "one"],
                                capture_output=True, text=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("recover its original bytes separately", json.loads(failed.stderr)["reason"])
        self.assertFalse((self.root / "old-missing").exists())

    def test_recovery_keeps_unavailable_unfinished_log_history_unknown(self):
        self.setup_task()
        self.run_check()
        session = json.loads(self.state.read_text())
        evidence = session["tasks"]["one"]["evidence"][0]
        evidence.update(status="BLOCKED", exit_code=None,
                        detail="Check started without a recorded completion; verify owned process before rerunning")
        session["events"] = [e for e in session["events"] if e["event"] != "check-finish"]
        Path(evidence["log"]).unlink()
        self.state.write_text(json.dumps(session))
        output = self.root / "unfinished.tgz"
        self.call("checkpoint", "--task", "one", "--output", output, "--next", "Inspect owned process")
        dest = self.root / "unfinished-recovery"
        result = self.run_generated_recovery(output, dest)
        self.assertEqual(result["unavailable_logs"],
                         [{"task": "one", "evidence_index": 0, "creation_history": "unknown"}])
        recovered = json.loads((dest / "state.json").read_text())
        record = recovered["tasks"]["one"]["evidence"][0]
        self.assertFalse(Path(record["log"]).exists())
        original = copy.deepcopy(evidence)
        original["log"] = record["log"]
        self.assertEqual(record, original)

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
