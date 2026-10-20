#!/usr/bin/env python3
"""Restore a trusted Lean checkpoint into a NEW directory. Python 3.10+, Git, POSIX."""
import argparse
import copy
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile

from lean import Denied, file_sha256, git, hash_stream, require, save, snapshot


def members(tar):
    """Validate before writing, including ancestors that could redirect extraction."""
    entries = {}
    roots = {"history.bundle", "state.json", "index.patch", "resume.md", "log-manifest.json"}
    for member in tar.getmembers():
        name = member.name
        path = PurePosixPath(name)
        require(bool(path.parts) and name == str(path) and not path.is_absolute() and
                all(p not in ("", ".", "..") and p.lower() != ".git" for p in path.parts),
                f"Unsafe archive member: {name}")
        require(name not in entries, f"Duplicate archive member: {name}")
        require(name in roots or path.parts[0] in ("logs", "worktree"),
                f"Unsupported archive member: {name}")
        require(member.isfile() or member.isdir() or
                (member.issym() and path.parts[0] == "worktree" and len(path.parts) > 1),
                f"Unsupported special archive member: {name}")
        require(name not in roots or member.isfile(), f"Required member is not regular: {name}")
        entries[name] = member
    for name in entries:
        for parent in PurePosixPath(name).parents:
            if str(parent) in entries:
                require(entries[str(parent)].isdir(), f"Nondirectory archive parent: {parent}")
    for required in ("history.bundle", "state.json", "index.patch", "resume.md"):
        require(required in entries, f"Missing required archive member: {required}")
    return entries


def log_mapping(tar, entries, session):
    expected = {(task_id, index): evidence for task_id, task in session["tasks"].items()
                for index, evidence in enumerate(task["evidence"])}
    has_manifest = "log-manifest.json" in entries
    if has_manifest:
        manifest = json.load(tar.extractfile(entries["log-manifest.json"]))
        require(manifest.get("version") == 1, "Unsupported log manifest version")
        refs = manifest["references"]
    else:
        # Old archives used basenames and captured only the selected task's logs.
        sources = {}
        refs = []
        for (task_id, index), evidence in expected.items():
            member = "logs/" + Path(evidence["log"]).name
            previous = sources.setdefault(member, evidence["log"])
            require(previous == evidence["log"], f"Ambiguous legacy log basename: {member}")
            require(member in entries, f"Legacy archive lacks log {evidence['log']}; "
                    "recover its original bytes separately. No evidence was invented")
            refs.append({"task": task_id, "evidence_index": index, "source": evidence["log"],
                         "availability": "available", "member": member})
    mapped, seen, digests = [], set(), {}
    for ref in refs:
        key = (ref["task"], ref["evidence_index"])
        require(key in expected and key not in seen, "Invalid or repeated log manifest reference")
        seen.add(key)
        require(ref["source"] == expected[key]["log"], "Log manifest source differs from ledger")
        if ref["availability"] == "available":
            member = ref["member"]
            require(member.startswith("logs/") and member in entries and entries[member].isfile(),
                    f"Missing regular log member: {member}")
            if has_manifest:
                if member not in digests:
                    with tar.extractfile(entries[member]) as stream:
                        digests[member] = hash_stream(stream)
                require(digests[member] == ref["sha256"],
                        f"Archived log digest differs: {member}")
            mapped.append((key, member))
        else:
            require(ref["availability"] == "unavailable" and ref.get("creation_history") == "unknown",
                    "Unsupported unavailable-log declaration")
            mapped.append((key, None))
    require(seen == set(expected), "Log manifest does not cover the embedded session")
    return mapped


def extract_member(tar, member, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if member.issym():
        destination.symlink_to(member.linkname)
    else:
        with tar.extractfile(member) as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target)
        destination.chmod(member.mode & 0o777)


def restore(archive, destination, task_id):
    archive = Path(archive).resolve()
    destination = Path(destination).absolute()
    require(not os.path.lexists(destination), "Recovery destination already exists; use a NEW directory")
    require(destination.parent.is_dir(), "Recovery destination parent must already exist")
    destination = destination.parent.resolve() / destination.name
    digest = file_sha256(archive)
    with tarfile.open(archive) as tar:
        entries = members(tar)
        session = json.load(tar.extractfile(entries["state.json"]))
        require(session.get("version") == 2, "Unsupported ledger version; preserve the archive unchanged")
        require(task_id in session["tasks"], "Selected task absent from checkpoint ledger")
        task = session["tasks"][task_id]
        require(task["work_state"] == "PAUSED" and "checkpoint_snapshot" in task,
                "Selected task has no paused checkpoint")
        saved = task["checkpoint_snapshot"]
        mapping = log_mapping(tar, entries, session)
        worktree = {name[len("worktree/"):]: member for name, member in entries.items()
                    if name.startswith("worktree/") and not member.isdir()}
        require(set(worktree) <= set(saved["files"]), "Worktree member absent from saved snapshot")
        destination.mkdir()  # Exclusive ownership; never extract over existing work.
        try:
            shutil.copyfile(archive, destination / "checkpoint.tar.gz")
            require(file_sha256(destination / "checkpoint.tar.gz") == digest,
                    "Archive changed during recovery; retry after inspecting the source")
            bundle = destination / "history.bundle"
            extract_member(tar, entries["history.bundle"], bundle)
            repo = destination / "repo"
            git(destination, "clone", "--quiet", "--no-checkout", str(bundle), str(repo))
            for key, value in (("core.autocrlf", "false"), ("core.filemode", "true"),
                               ("core.symlinks", "true")):
                git(repo, "config", key, value)
            if saved["branch"] == "HEAD":
                git(repo, "checkout", "--quiet", "--detach", saved["head"])
            else:
                git(repo, "check-ref-format", "--branch", saved["branch"])
                git(repo, "checkout", "--quiet", "-B", saved["branch"], saved["head"])
            # Start with the HEAD index, but replace the entire worktree to retain deletions.
            for child in repo.iterdir():
                if child.name == ".git":
                    continue
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            for name, member in worktree.items():
                extract_member(tar, member, repo / name)
            patch = tar.extractfile(entries["index.patch"]).read()
            if patch:
                patch_path = destination / "index.patch"
                patch_path.write_bytes(patch)
                git(repo, "apply", "--cached", "--binary", str(patch_path))
            require(snapshot(repo)["fingerprint"] == saved["fingerprint"],
                    "Restored fingerprint differs; archive may lack files or this platform cannot restore identity")
            recovered = copy.deepcopy(session)
            unavailable = []
            for (tid, index), member in mapping:
                evidence = recovered["tasks"][tid]["evidence"][index]
                if member is None:
                    member = f"unavailable-logs/{len(unavailable)}.log"
                    unavailable.append({"task": tid, "evidence_index": index,
                                        "creation_history": "unknown"})
                elif not (destination / member).exists():
                    extract_member(tar, entries[member], destination / member)
                evidence["log"] = str(destination / member)
            target = recovered["tasks"][task_id]
            # Historical checkout references intentionally remain historical.
            target["repo"] = str(repo)
            target["checkpoint"].update(path=str(destination / "checkpoint.tar.gz"), sha256=digest,
                                        durability="unconfirmed")
            target["checkpoint"].pop("location", None)
            for name in ("state.json", "resume.md", "log-manifest.json"):
                if name in entries:
                    extract_member(tar, entries[name], destination / ("archived-" + name))
            save(destination / "state.json", recovered)
            return {"state": str(destination / "state.json"), "repo": str(repo),
                    "fingerprint": saved["fingerprint"], "archive_sha256": digest,
                    "unavailable_logs": unavailable, "durability": "unconfirmed"}
        except BaseException:
            shutil.rmtree(destination)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--task", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(restore(args.archive, args.destination, args.task), indent=2))
        return 0
    except (Denied, OSError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
