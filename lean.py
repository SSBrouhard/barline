#!/usr/bin/env python3
"""Lean task harness. Python 3.10+, Git, POSIX. No services or dependencies."""
import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import shutil
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import time


class Denied(Exception):
    pass


class CheckpointPublished(Denied):
    def __init__(self, archive, digest, error):
        self.archive, self.sha256 = str(archive), digest
        super().__init__(f"Checkpoint archive published at {archive}; recover it before retrying. "
                         f"Ledger publication not confirmed: {error}")


def hash_stream(stream):
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def file_sha256(path):
    with open(path, "rb") as stream:
        return hash_stream(stream)


def require(condition, message):
    if not condition:
        raise Denied(message)


def git(repo, *args, readonly=False):
    # Observation must not perform Git's optional index refresh or take its locks.
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0") if readonly else None
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=env)
    require(p.returncode == 0, p.stderr.decode(errors="replace").strip() or "Git failed")
    return p.stdout


def snapshot(repo, readonly=False):
    repo = Path(repo).resolve()
    def read_git(*args):
        return git(repo, *args, readonly=readonly)
    require(read_git("rev-parse", "--show-toplevel").decode().strip() == str(repo),
            "Use the repository root")
    head = read_git("rev-parse", "HEAD").decode().strip()
    branch = read_git("rev-parse", "--abbrev-ref", "HEAD").decode().strip()
    names = sorted(set(read_git("ls-files", "-z", "--cached", "--others",
                           "--exclude-standard").split(b"\0")) - {b""})
    h = hashlib.sha256((head + "\0" + branch).encode())
    for raw in names:
        path = repo / os.fsdecode(raw)
        h.update(len(raw).to_bytes(8, "big") + raw)
        if path.is_symlink():
            digest = hashlib.sha256(os.fsencode(os.readlink(path))).digest()
            mode = "link"
        elif path.is_file():
            digest = bytes.fromhex(file_sha256(path))
            mode = str(path.stat().st_mode & 0o777)
        elif not path.exists():
            digest, mode = hashlib.sha256(b"").digest(), "deleted"
        else:
            raise Denied("Submodules/special files need a repository-specific checkpoint adapter")
        h.update(mode.encode() + b"\0" + digest)
    status = read_git("status", "--porcelain=v1", "--untracked-files=all").decode(errors="replace")
    # Include index changes too, even when worktree contents are identical.
    h.update(read_git("diff", "--cached", "--binary", "HEAD"))
    return {"head": head, "branch": branch, "fingerprint": h.hexdigest(), "dirty": bool(status),
            "status": status, "files": [os.fsdecode(x) for x in names]}


def save(path, value):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".lean-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def locked(path):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Denied("Lead ledger is busy; retry after the current atomic action")
        yield path


def admit(task):
    require(not task.get("superseded_by") and not task.get("contract_conflicts"),
            "Contract is conflicted or superseded; start a linked follow-up task")
    require(task["work_state"] == "ACTIVE", "Task is paused; inspect checkpoint before resuming")
    require(task["failed_hypotheses"] < 2, "Two failed repair hypotheses; diagnosis checkpoint required")


def contract(c):
    for field in ("goal", "owner_request", "mode", "checks", "required_external",
                  "must_happen", "must_never", "non_goals", "policy_sources"):
        require(field in c, f"Contract missing {field}")
    require(c["goal"] and c["owner_request"] and c["policy_sources"], "Goal, request and policy source required")
    require(c["mode"] in ("local", "pr"), "Mode must be local or pr")
    require(isinstance(c["checks"], dict) and c["checks"], "At least one real check required")
    for name, check in c["checks"].items():
        require(name.replace("-", "").replace("_", "").isalnum(), "Unsafe check name")
        require(isinstance(check.get("argv"), list) and check["argv"] and
                all(isinstance(a, str) for a in check["argv"]), "Check needs argv, no shell string")
        timeout = check.get("timeout_seconds")
        require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
                "Check timeout must be a positive finite number")
        require(type(check.get("expected_exit", 0)) is int, "Expected exit must be integer")
    require(isinstance(c["required_external"], list), "External requirements must be a list")
    if c["mode"] == "pr":
        require({"repo-ci", "final-adversarial-review"} <= set(c["required_external"]),
                "PR mode requires repository CI and final adversarial review")
    return c


def gate(t, now=None):
    now = snapshot(t["repo"]) if now is None else now
    # A newer attempt supersedes the old one even if its source no longer matches.
    attempts = {e["check"]: e for e in t["evidence"]}
    latest = {name: e for name, e in attempts.items() if e["fingerprint"] == now["fingerprint"]}
    missing = [name for name in t["contract"]["checks"] if name not in latest]
    failed = [name for name, e in latest.items() if e["status"] == "FAIL"]
    blocked = [name for name, e in latest.items() if e["status"] == "BLOCKED"]
    inconclusive = [name for name, e in latest.items() if e["status"] == "INCONCLUSIVE"]
    external = []
    for kind in t["contract"]["required_external"]:
        receipts = [r for r in t["external"] if r["kind"] == kind and
                    r["head"] == now["head"] and r.get("fingerprint") == now["fingerprint"]]
        receipt = receipts[-1] if receipts else None
        if receipt and receipt["result"] == "FAIL":
            failed.append(kind)
        if not receipt or receipt["result"] != "PASS" or not receipt.get("verified_by"):
            external.append(kind)
        elif kind == "repo-ci" and now["dirty"]:
            external.append(kind)
    reasons = []
    contract_blocked = bool(t.get("contract_conflicts") or t.get("superseded_by"))
    if t.get("contract_conflicts"):
        reasons.append("Declared contract conflict; reconcile in a new task using start --follows")
    if t.get("superseded_by"):
        reasons.append("Contract superseded by task " + t["superseded_by"])
    if external:
        reasons.append("External requirements need independent verification: " + ", ".join(external))
    if t["scope_outcome"] != "met" or t["scope_fingerprint"] != now["fingerprint"]:
        reasons.append("Lead must assess the frozen acceptance bar on this exact tree")
    status = "FAIL" if failed else "BLOCKED" if blocked or external or contract_blocked else "INCONCLUSIVE" if missing or reasons or inconclusive else "PASS"
    return {"validation": status, "scope": t["scope_outcome"], "work_state": t["work_state"],
            "head": now["head"], "fingerprint": now["fingerprint"], "missing": missing,
            "failed": failed, "blocked": blocked, "inconclusive": inconclusive, "reasons": reasons,
            "follows": t.get("follows"), "superseded_by": t.get("superseded_by"),
            "contract_conflicts": t.get("contract_conflicts", []),
            "external_receipts": t["external"],
            "assurance": "Local checks are executed. Scope and external verification are lead-recorded claims; external sources are not fetched by this CLI."}


@contextlib.contextmanager
def observation_task(state, task, repo=None):
    """Validate exact task identity under its existing read-only ledger lock."""
    path = Path(state).resolve()
    # No mkdir, append-open or save: absence of the existing lock is unavailable.
    with open(str(path) + ".lock", "rb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Denied("Lead ledger is busy; retry observation after its atomic action")
        raw = path.read_bytes()
        s = json.loads(raw)
        require(type(s) is dict and type(s.get("version")) is int and s["version"] == 2,
                "Unsupported ledger version; preserve it unchanged")
        require(type(s.get("tasks")) is dict and task in s["tasks"], "Exact enrolled task is missing")
        t = s["tasks"][task]
        require(type(t) is dict, "Invalid task record")
        contract(t["contract"])
        require(t["work_state"] in ("ACTIVE", "PAUSED") and
                t["scope_outcome"] in ("unknown", "not-met", "met") and
                type(t["failed_hypotheses"]) is int and t["failed_hypotheses"] >= 0,
                "Invalid task state")
        require(type(t["evidence"]) is list and type(t["external"]) is list, "Invalid evidence records")
        require(type(t.get("contract_conflicts", [])) is list and
                ("superseded_by" not in t or isinstance(t["superseded_by"], str) and bool(t["superseded_by"])),
                "Invalid task conflict or successor record")
        for evidence in t["evidence"]:
            require(evidence["check"] in t["contract"]["checks"] and
                    evidence["status"] in ("PASS", "FAIL", "BLOCKED", "INCONCLUSIVE") and
                    isinstance(evidence["fingerprint"], str), "Invalid check evidence")
        for receipt in t["external"]:
            require(receipt["kind"] in t["contract"]["required_external"] and
                    receipt["result"] in ("PASS", "FAIL", "BLOCKED", "INCONCLUSIVE"),
                    "Invalid external receipt")
        target = Path(t["repo"]).resolve()
        require(repo is None or target == Path(repo).resolve(), "Enrolled checkout differs from task")
        repos = [str(Path(value["repo"]).resolve()) for value in s["tasks"].values()]
        require(not any(path.is_relative_to(Path(root)) for root in repos),
                "Ledger must be outside every target checkout")
        require(target.is_dir(), "Enrolled checkout is unavailable")
        yield path, raw, t, target, repos


def task_identity(state, task, repo):
    """No-write turn validation; no Git, source traversal or acceptance claim."""
    with observation_task(state, task, repo) as (path, raw, t, target, repos):
        return {"task": task, "repo": str(target), "state": str(path), "repos": repos}


def observe(state, task, repo=None):
    """No-write observation. Call in a deadline-supervised process for hook use."""
    with observation_task(state, task, repo) as (path, raw, t, target, repos):
        before = snapshot(target, readonly=True)
        result = gate(t, now=before)
        pause_valid = False
        if t["work_state"] == "PAUSED":
            cp = t.get("checkpoint", {})
            try:
                pause_valid = bool(t.get("next", "").strip() and
                    t.get("checkpoint_snapshot", {}).get("fingerprint") == before["fingerprint"] and
                    cp.get("sha256") and file_sha256(cp["path"]) == cp["sha256"])
            except OSError:
                # A broken archive prevents certifying pause, but the lead may
                # still need a truthful input/blocker handoff about its recovery.
                pause_valid = False
        after = snapshot(target, readonly=True)
        require(before == after and path.read_bytes() == raw,
                "Repository or ledger changed during observation; retry without starting duplicate checks")
        result.update(task=task, repo=str(target), state=str(path), repos=repos,
                      ledger_digest=hashlib.sha256(raw).hexdigest(),
                      failed_hypotheses=t["failed_hypotheses"], pause_valid=pause_valid,
                      next=t.get("next"))
        return result


def execute(s, t, name, state_path):
    admit(t)
    require(name in t["contract"]["checks"], "Command is outside frozen contract")
    c = t["contract"]["checks"][name]
    timeout = c["timeout_seconds"]
    before = snapshot(t["repo"])
    rid = f"check-{len(s['events'])}"
    logs = state_path.parent / (state_path.stem + "-logs")
    logs.mkdir(exist_ok=True)
    log = logs / (rid + ".log")
    e = {"attempt_id": rid, "check": name, "status": "BLOCKED", "exit_code": None, "argv": c["argv"],
         "expected_exit": c.get("expected_exit", 0), "fingerprint": before["fingerprint"],
         "head": before["head"], "log": str(log),
         "detail": "Check started without a recorded completion; verify owned process before rerunning",
         "environment": {"python": sys.version.split()[0], "platform": sys.platform}, "time": time.time()}
    t["evidence"].append(e)
    s["events"].append({"event": "check-start", "id": rid, "check": name})
    # Persist uncertainty before launch so a killed lead cannot expose an older PASS.
    save(state_path, s)
    rc, status, detail = None, "BLOCKED", ""
    try:
        with log.open("wb") as out:
            p = subprocess.Popen(c["argv"], cwd=t["repo"], stdout=out, stderr=subprocess.STDOUT,
                                 start_new_session=True)
            try:
                e["process_group"] = p.pid
                save(state_path, s)
                rc = p.wait(timeout=timeout)
                status = "PASS" if rc == c.get("expected_exit", 0) else "FAIL"
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
                detail = "Timed out or interrupted; owned process group terminated"
            except OSError:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
                raise
    except OSError as exc:
        detail = str(exc)
    finally:
        after = snapshot(t["repo"])
        if before["fingerprint"] != after["fingerprint"]:
            status, detail = "INCONCLUSIVE", "Check changed source; rerun after inspecting changes"
        e.update(status=status, exit_code=rc, detail=detail)
        s["events"].append({"event": "check-finish", "id": rid, "status": status})
        save(state_path, s)
    return e


def checkpoint_logs(s, stage):
    """Capture the embedded ledger's log closure, keeping physical aliases together."""
    references, physical, members = [], {}, set()
    finished = {e["id"] for e in s["events"] if e.get("event") == "check-finish"}
    for task_id, task in s["tasks"].items():
        for index, evidence in enumerate(task["evidence"]):
            source = Path(evidence["log"])
            ref = {"task": task_id, "evidence_index": index, "source": str(source)}
            try:
                require(stat.S_ISREG(source.stat().st_mode), f"Check log is not a regular file: {source}")
                with source.open("rb") as stream:
                    info = os.fstat(stream.fileno())
                    require(stat.S_ISREG(info.st_mode), f"Check log is not a regular file: {source}")
                    key = (info.st_dev, info.st_ino)
                    if key not in physical:
                        member = "logs/" + source.name
                        suffix = 1
                        while member in members:
                            member = f"logs/{suffix}-{source.name}"
                            suffix += 1
                        members.add(member)
                        captured = stage / member
                        captured.parent.mkdir(exist_ok=True)
                        with captured.open("wb") as destination:
                            shutil.copyfileobj(stream, destination)
                        physical[key] = {"member": member, "sha256": file_sha256(captured)}
                    ref.update(availability="available", **physical[key])
            except FileNotFoundError:
                attempt = evidence.get("attempt_id", source.stem)
                unfinished = (evidence.get("status") == "BLOCKED" and
                              evidence.get("exit_code") is None and attempt not in finished and
                              evidence.get("detail") ==
                              "Check started without a recorded completion; verify owned process before rerunning")
                require(unfinished, f"Completed check log missing for task {task_id}, "
                        f"evidence {index}: {source}; recover the log before checkpointing")
                ref.update(availability="unavailable", creation_history="unknown",
                           reason="Unfinished attempt log absent; it may never have opened or may have been deleted")
            references.append(ref)
    manifest = {"version": 1, "references": references}
    (stage / "log-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def checkpoint(s, t, output, note):
    output = Path(output).resolve()
    repo = Path(t["repo"])
    require(not output.is_relative_to(repo), "Checkpoint must live outside the source repository")
    require(not output.exists(), "Checkpoint already exists; use a new path")
    current = snapshot(repo)
    # Archive failure must not leak a PAUSED state into the caller's live session.
    staged = copy.deepcopy(s)
    task_id = next(key for key, value in s["tasks"].items() if value is t)
    target = staged["tasks"][task_id]
    target.update(work_state="PAUSED", checkpoint_snapshot=current, next=note,
                  checkpoint={"path": str(output), "durability": "unconfirmed"})
    output.parent.mkdir(parents=True, exist_ok=True)
    published, digest = False, None
    try:
        with tempfile.TemporaryDirectory() as td:
            stage = Path(td)
            manifest = checkpoint_logs(staged, stage)
            git(repo, "bundle", "create", str(stage / "history.bundle"), "--all", "HEAD")
            (stage / "state.json").write_text(json.dumps(staged, indent=2))
            (stage / "resume.md").write_text(
                f"# Resume {target['contract']['goal']}\n\nWork: PAUSED. Validation: {gate(target)['validation']}.\n"
                f"Head: {current['head']}\nFingerprint: {current['fingerprint']}\n"
                f"Failed hypotheses: {target['failed_hypotheses']}.\nNext: {note}\n\n"
                "Read state.json for the frozen contract, original request, evidence and session history.\n"
                "A local archive is not confirmed durable. Preserve it in an authorized persistent destination, "
                "then record its receipt using `durable`. Do not push merely to save state.\n"
                "Recover only an archive from a trusted source: it contains Git history and working files. "
                "Run the following shell block with the NEW destination as its first positional argument. "
                "If the archive moved, pass its current path as the second argument. "
                "Use the companion recover.py beside this version of lean.py; update its tool path if the harness moved.\n\n"
                "```sh\n"
                ': "${1:?Pass a NEW recovery destination as argument 1}"\n'
                f"archive={shlex.quote(str(output))}\n"
                'if [ "$#" -ge 2 ]; then archive=$2; fi\n'
                f"python3 {shlex.quote(str(Path(__file__).resolve().with_name('recover.py')))} "
                f'"$archive" "$1" --task {shlex.quote(task_id)}\n```\n\n'
                "Recovery verifies the original fingerprint, preserves the archive bytes, and creates "
                "repo/, state.json and remapped logs in that new directory. Only the selected checkout "
                "is restored. Other task checkout paths remain historical; all available log paths are remapped. "
                "Unavailable unfinished logs remain absent with unknown creation history. "
                "Read the recovered frozen contract/history, confirm authorized persistent storage using durable, "
                "then compare with `resume --expect FINGERPRINT`. Prior notes supply no new authority.\n")
            (stage / "index.patch").write_bytes(git(repo, "diff", "--cached", "--binary", "HEAD"))
            fd, tmp = tempfile.mkstemp(dir=output.parent, prefix=".checkpoint-")
            os.close(fd)
            try:
                with tarfile.open(tmp, "w:gz") as tar:
                    for item in stage.iterdir():
                        tar.add(item, arcname=item.name)
                    for name in current["files"]:
                        p = repo / name
                        if p.exists() or p.is_symlink():
                            def regular_payload(info):
                                # Inode sharing is outside snapshot identity; store regular bytes.
                                if info.islnk():
                                    info.type, info.linkname, info.size = tarfile.REGTYPE, "", p.stat().st_size
                                return info
                            tar.add(p, arcname="worktree/" + name, recursive=False, filter=regular_payload)
                require(snapshot(repo)["fingerprint"] == current["fingerprint"], "Source changed while archiving; retry")
                with tarfile.open(tmp) as tar:
                    require("history.bundle" in tar.getnames() and "state.json" in tar.getnames(), "Incomplete archive")
                    digests = {}
                    for ref in manifest["references"]:
                        if ref["availability"] == "available":
                            member = ref["member"]
                            if member not in digests:
                                with tar.extractfile(member) as stream:
                                    digests[member] = hash_stream(stream)
                            require(digests[member] == ref["sha256"], "Archived log digest differs")
                os.replace(tmp, output)
                published = True
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        digest = file_sha256(output)
    except (Denied, OSError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
        if published:
            raise CheckpointPublished(output, digest, exc) from exc
        raise
    target["checkpoint"]["sha256"] = digest
    t.clear()
    t.update(target)
    return {"archive": str(output), "sha256": digest, "durability": "unconfirmed", "work_state": "PAUSED"}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", required=True, type=Path, help="One lead-owned session ledger OUTSIDE target repositories")
    sp = p.add_subparsers(dest="command", required=True)
    sp.add_parser("init")
    observation = sp.add_parser("observe", help="Supervised read-only gate query; never saves the ledger")
    observation.add_argument("--task", required=True)
    observation.add_argument("--timeout", type=float, default=5)
    start = sp.add_parser("start")
    start.add_argument("--repo", required=True, type=Path)
    start.add_argument("--contract", required=True, type=Path)
    start.add_argument("--task", required=True)
    start.add_argument("--follows", help="Preceding task replaced by this authorized material follow-up")
    for name in ("status", "gate", "run", "assess", "hypothesis", "park", "checkpoint", "durable", "resume", "receipt"):
        x = sp.add_parser(name)
        x.add_argument("--task", required=True)
        if name == "run": x.add_argument("--check", required=True)
        if name == "assess":
            x.add_argument("--outcome", choices=("met", "not-met", "unknown", "conflict"), required=True)
            x.add_argument("--evidence", required=True)
        if name == "hypothesis":
            x.add_argument("--description", required=True)
            x.add_argument("--result", choices=("failed", "supported"), required=True)
        if name in ("park", "checkpoint"): x.add_argument("--next", required=True)
        if name == "checkpoint": x.add_argument("--output", required=True, type=Path)
        if name == "durable": x.add_argument("--location", required=True)
        if name == "resume": x.add_argument("--expect", required=True)
        if name == "receipt":
            x.add_argument("--kind", required=True)
            x.add_argument("--reference", required=True)
            x.add_argument("--head", required=True)
            x.add_argument("--result", choices=("PASS", "FAIL", "BLOCKED", "INCONCLUSIVE"), required=True)
            x.add_argument("--verified-by", help="Named verification source actually consulted by the lead; do not infer from a claim")
    diagnose = sp.add_parser("diagnose")
    diagnose.add_argument("--task", required=True)
    diagnose.add_argument("--new-evidence", required=True)
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    if a.command == "observe":
        # Keep all traversal, JSON loading and Git work inside the bounded worker.
        sys.dont_write_bytecode = True
        from hooks.stop import supervise
        result = supervise({"operation": "observe", "state": str(a.state), "task": a.task}, a.timeout)
        print(json.dumps(result, indent=2))
        return 2 if result.get("decision") == "block" else 0
    try:
        with locked(a.state) as path:
            if a.command == "init":
                require(not path.exists(), "Session already exists; preserve its history")
                s = {"version": 2, "tasks": {}, "events": []}
                result = {"session": str(path), "version": s["version"]}
            else:
                s = json.loads(path.read_text())
                require(s.get("version") == 2,
                        "Unsupported ledger version; preserve the original and review conversion before continuing")
                if a.command == "start":
                    require(a.task not in s["tasks"], "Task already exists")
                    repo = a.repo.resolve()
                    require(not path.is_relative_to(repo), "Session ledger must be outside repository")
                    c = contract(json.loads(a.contract.read_text()))
                    preceding = None
                    if a.follows is not None:
                        require(a.follows in s["tasks"], "Preceding task does not exist")
                        preceding = s["tasks"][a.follows]
                        require(preceding["repo"] == str(repo), "Follow-up must use the preceding repository")
                        require(not preceding.get("superseded_by"), "Follow the current task, not a superseded task")
                        require(isinstance(c["owner_request"], str) and c["owner_request"].strip() and
                                c["owner_request"] != preceding["contract"]["owner_request"],
                                "Reference the actual follow-up authorization in owner_request")
                        require(preceding["contract"]["mode"] != "pr" or c["mode"] == "pr",
                                "Follow-up cannot downgrade PR obligations")
                        require(set(preceding["contract"]["required_external"]) <= set(c["required_external"]),
                                "Follow-up must retain required external obligations")
                    snap = snapshot(repo)
                    s["tasks"][a.task] = {"repo": str(repo), "contract": c, "base": snap,
                        "work_state": "ACTIVE",
                        "scope_outcome": "unknown", "scope_fingerprint": None, "evidence": [], "external": [],
                        "hypotheses": [], "failed_hypotheses": 0, "parked": []}
                    if preceding is not None:
                        s["tasks"][a.task]["follows"] = a.follows
                        preceding["superseded_by"] = a.task
                    result = {"task": a.task, "head": snap["head"], "goal": c["goal"]}
                elif a.command == "diagnose":
                    t = s["tasks"][a.task]
                    require(t["work_state"] == "PAUSED", "Checkpoint before diagnosis")
                    require(a.new_evidence.strip() and a.new_evidence not in t.get("diagnoses", []), "New causal evidence required")
                    t.setdefault("diagnoses", []).append(a.new_evidence)
                    t["failed_hypotheses"] = 0
                    result = {"new_evidence": a.new_evidence, "history": t["hypotheses"],
                              "assurance": "Lead-recorded evidence, not automatically verified"}
                else:
                    t = s["tasks"][a.task]
                    if a.command in ("status", "gate"):
                        result = gate(t)
                    elif a.command == "run": result = execute(s, t, a.check, path)
                    elif a.command == "assess":
                        require(a.evidence.strip(), "Acceptance or conflict evidence required")
                        if a.outcome == "met":
                            require(not t.get("contract_conflicts") and not t.get("superseded_by"),
                                    "Contract is conflicted or superseded; start a linked follow-up task")
                        if a.outcome == "conflict":
                            t.setdefault("contract_conflicts", []).append({"evidence": a.evidence, "time": time.time()})
                        else:
                            t.update(scope_outcome=a.outcome, scope_evidence=a.evidence,
                                     scope_fingerprint=snapshot(t["repo"])["fingerprint"])
                        result = gate(t)
                    elif a.command == "hypothesis":
                        require(a.description not in [h["description"] for h in t["hypotheses"]], "Hypothesis already recorded")
                        t["hypotheses"].append({"description": a.description, "result": a.result})
                        if a.result == "failed": t["failed_hypotheses"] += 1
                        result = {"failed_hypotheses": t["failed_hypotheses"]}
                    elif a.command == "park":
                        t["parked"].append(a.next)
                        result = {"parked": a.next}
                    elif a.command == "checkpoint": result = checkpoint(s, t, a.output, a.next)
                    elif a.command == "durable":
                        cp = t["checkpoint"]
                        require(hashlib.sha256(Path(cp["path"]).read_bytes()).hexdigest() == cp["sha256"], "Archive changed")
                        cp.update(durability="operator-confirmed", location=a.location)
                        result = cp
                    elif a.command == "resume":
                        require(t["work_state"] == "PAUSED", "Only paused work can resume")
                        snap = snapshot(t["repo"])
                        require(a.expect == snap["fingerprint"] == t["checkpoint_snapshot"]["fingerprint"],
                                "Checkpoint tree differs; reconcile before resume")
                        require(t["checkpoint"]["durability"] == "operator-confirmed", "Persistent checkpoint receipt required")
                        require(t["failed_hypotheses"] < 2, "Diagnosis with new causal evidence required; history preserved")
                        t["work_state"] = "ACTIVE"
                        result = {"work_state": "ACTIVE", "next": t["next"]}
                    elif a.command == "receipt":
                        snap = snapshot(t["repo"])
                        require(a.kind in t["contract"]["required_external"], "Receipt is outside required external checks")
                        require(a.head == snap["head"], "External receipt must match candidate HEAD")
                        if a.verified_by:
                            require(a.reference.startswith(("https://", "file://", "/")), "Verified evidence needs an actual URL or absolute artifact path")
                            require(a.verified_by.strip(), "Name the actual verification source")
                        t["external"].append({"kind": a.kind, "reference": a.reference, "head": a.head,
                                              "fingerprint": snap["fingerprint"], "verified_by": a.verified_by,
                                              "result": a.result, "assurance": "Lead-recorded external verification; CLI does not fetch or authenticate source"})
                        result = gate(t)
            s["events"].append({"event": a.command, "time": time.time()})
            try:
                save(path, s)
            except OSError as exc:
                if a.command == "checkpoint":
                    raise CheckpointPublished(result["archive"], result["sha256"], exc) from exc
                raise
            print(json.dumps(result, indent=2))
            if a.command == "gate": return 0 if result["validation"] == "PASS" else 1
            if a.command == "run": return 0 if result["status"] == "PASS" else 1
            return 0
    except (Denied, OSError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
        error = {"status": "BLOCKED", "reason": str(exc)}
        if isinstance(exc, CheckpointPublished):
            error.update(archive=exc.archive, sha256=exc.sha256, ledger_publication="unconfirmed")
        print(json.dumps(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
