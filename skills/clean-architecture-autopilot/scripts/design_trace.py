#!/usr/bin/env python3
"""Reconcile planned P2 files, P4 artifacts, and baseline changes.

A changed task file is traceable only when its P2 task and P4 artifact agree, or
when a recorded design_amendment explicitly approves the exact change.
"""
import argparse
import json
import os
import subprocess
import sys


_MISSING = object()


def _git_porcelain_v1_paths(root):
    """Return ``(status, current_path)`` records from NUL-delimited porcelain v1."""
    completed = subprocess.run(
        ["git", "-C", root, "status", "--porcelain=v1", "-z",
         "--untracked-files=all"],
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        error = os.fsdecode(completed.stderr).strip()
        raise ValueError(error or "cannot read git status")

    records = completed.stdout.split(b"\0")
    paths = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 3 or record[2:3] != b" ":
            continue
        status = record[:2].decode("ascii", errors="replace")
        paths.append((status, os.fsdecode(record[3:])))
        if (b"R" in record[:2] or b"C" in record[:2]) and index < len(records):
            index += 1
    return paths


def load_json(path):
    """Read and return a JSON document from *path*."""
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def _path_error(path):
    """Return why *path* is not a canonical project-relative POSIX path."""
    if not isinstance(path, str):
        return "must be a string"
    if not path:
        return "must not be empty"
    if path != path.strip():
        return "must not have leading or trailing whitespace"
    if "\\" in path:
        return "must use POSIX separators"
    if path.startswith("/"):
        return "must be project-relative"
    if "//" in path:
        return "must not contain double slashes"
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return "must not contain empty, '.' or '..' path segments"
    return None


def _canonical_path(path):
    """Return a path only when it is already canonical; never normalize input."""
    return path if _path_error(path) is None else None


def _validate_files(value, field):
    """Validate a non-empty files list and return its deterministic file set."""
    if not isinstance(value, list) or not value:
        return [], [f"{field} must be a non-empty list[str]"]

    errors = []
    files = []
    for index, path in enumerate(value):
        error = _path_error(path)
        if error:
            errors.append(f"{field}[{index}] {error}")
        else:
            files.append(path)
    return sorted(set(files)), errors


def _task_id_error(task_id):
    if not isinstance(task_id, str) or not task_id or task_id != task_id.strip():
        return "must be a non-empty string without surrounding whitespace"
    return None


def _normalized_task_id(record, prefix):
    """Validate compatible task ID aliases and return one internal ID."""
    has_id = "id" in record
    has_task_id = "task_id" in record
    if not has_id and not has_task_id:
        return None, [f"{prefix} must include id or task_id"]
    if has_id and has_task_id and record["id"] != record["task_id"]:
        return None, [f"{prefix}.id and .task_id must match"]
    task_id = record["id"] if has_id else record["task_id"]
    error = _task_id_error(task_id)
    if error:
        return None, [f"{prefix}.id or .task_id {error}"]
    return task_id, []


def _task_files(task, prefix):
    """Return P2 task files and errors without silently resolving aliases."""
    has_files = "files" in task
    has_files_touched = "files_touched" in task
    if has_files and has_files_touched:
        files, file_errors = _validate_files(task["files"], f"{prefix}.files")
        touched, touched_errors = _validate_files(
            task["files_touched"], f"{prefix}.files_touched")
        errors = file_errors + touched_errors
        if not errors and files != touched:
            errors.append(f"{prefix}.files and .files_touched must match")
        return touched, errors
    value = task.get("files_touched", task.get("files", _MISSING))
    return _validate_files(value, f"{prefix}.files")


def _collect_p2_tasks(p2):
    """Return P2 tasks, schema errors, and whether a DAG was declared."""
    if not isinstance(p2, dict):
        return None, ["P2 artifact must be an object"], False
    if "dag_tasks" not in p2:
        return None, [], False
    if not isinstance(p2["dag_tasks"], list):
        return None, ["P2 dag_tasks must be a list"], False
    if not p2["dag_tasks"]:
        return [], ["P2 dag_tasks must not be empty"], True

    errors = []
    tasks = []
    task_ids = set()
    for index, task in enumerate(p2["dag_tasks"]):
        prefix = f"P2 dag_tasks[{index}]"
        if not isinstance(task, dict):
            errors.append(f"{prefix} must be an object")
            continue
        task_id, task_id_errors = _normalized_task_id(task, prefix)
        if task_id_errors:
            errors.extend(task_id_errors)
            continue
        if task_id in task_ids:
            errors.append(f"duplicate P2 task_id: {task_id}")
            continue
        task_ids.add(task_id)
        files, file_errors = _task_files(task, prefix)
        errors.extend(file_errors)
        tasks.append({"task_id": task_id, "files": files})
    return tasks, errors, True


def collect_p2_tasks(p2):
    """Return canonical P2 DAG tasks, or None when the artifact has no DAG."""
    tasks, _, has_dag = _collect_p2_tasks(p2)
    return tasks if has_dag else None


def _artifact_records(value):
    """Yield only the two supported P4 artifact shapes without recursive discovery."""
    if isinstance(value, dict) and ("id" in value or "task_id" in value):
        yield value
        return

    if isinstance(value, dict) and set(value) == {"tasks"}:
        tasks = value["tasks"]
        if isinstance(tasks, list) and tasks:
            yield from tasks
            return

    # Keep every invalid supplied value for ``check`` to reject; never discard it.
    yield value


def _validate_p4_artifact(artifact, index):
    prefix = f"P4 artifact[{index}]"
    if not isinstance(artifact, dict):
        return None, [f"{prefix} must be an object"]
    if ("id" in artifact or "task_id" in artifact) and "tasks" in artifact:
        return None, [
            f"{prefix} must not combine a direct record with a tasks wrapper"]
    task_id, task_id_errors = _normalized_task_id(artifact, prefix)
    if task_id_errors:
        return None, task_id_errors
    files, file_errors = _validate_files(
        artifact.get("files", _MISSING), f"{prefix}.files")
    if file_errors:
        return None, file_errors
    return {"task_id": task_id, "files": files}, []


def collect_p4_artifacts(run_dir):
    """Read P4 task artifacts from ``<run_dir>/artifacts`` JSON files."""
    artifacts_dir = os.path.join(run_dir, "artifacts")
    if not os.path.isdir(artifacts_dir):
        return []
    artifacts = []
    for directory, _, names in os.walk(artifacts_dir):
        for name in sorted(names):
            if not (name.startswith("p4-") and name.endswith(".json")):
                continue
            path = os.path.join(directory, name)
            try:
                artifacts.extend(_artifact_records(load_json(path)))
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError(f"cannot read P4 artifact {path}: {error}") from error
    return artifacts


def collect_amendments(events):
    """Extract every design_amendment detail for later schema validation."""
    amendments = []
    for event in events or []:
        if isinstance(event, dict) and event.get("event") == "design_amendment":
            amendments.append(event.get("detail", _MISSING))
    return amendments


def _record_hash(record):
    if not isinstance(record, dict):
        return None
    hash_value = record.get("hash", _MISSING)
    sha256_value = record.get("sha256", _MISSING)
    if (hash_value is not _MISSING and sha256_value is not _MISSING
            and hash_value != sha256_value):
        return None
    value = hash_value if hash_value is not _MISSING else sha256_value
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in value)):
        return None
    return value


def _hash_alias_error(record):
    if (isinstance(record, dict) and "hash" in record and "sha256" in record
            and record["hash"] != record["sha256"]):
        return "hash and .sha256 must match"
    return None


def _validate_preexisting_evidence(manifest, events):
    """Return schema errors for provided manifest and baseline_accept evidence."""
    errors = []
    if not isinstance(manifest, dict):
        errors.append("manifest must be an object")
        preexisting = _MISSING
    else:
        preexisting = manifest.get("preexisting_files", _MISSING)

    manifest_hashes = set()
    if preexisting is not _MISSING:
        if not isinstance(preexisting, list):
            errors.append("manifest preexisting_files must be a list")
        else:
            manifest_paths = set()
            for index, record in enumerate(preexisting):
                prefix = f"manifest preexisting_files[{index}]"
                if not isinstance(record, dict):
                    errors.append(f"{prefix} must be an object")
                    continue
                path = record.get("path")
                path_error = _path_error(path)
                if path_error:
                    errors.append(f"{prefix}.path {path_error}")
                elif path in manifest_paths:
                    errors.append(f"conflicting manifest preexisting_files path: {path}")
                else:
                    manifest_paths.add(path)
                alias_error = _hash_alias_error(record)
                if alias_error:
                    errors.append(f"{prefix}.{alias_error}")
                digest = _record_hash(record)
                if digest is None:
                    errors.append(
                        f"{prefix}.hash must be a 64-character hexadecimal SHA-256")
                elif path_error is None:
                    manifest_hashes.add((path, digest))

    for index, event in enumerate(events or []):
        if not isinstance(event, dict) or event.get("event") != "baseline_accept":
            continue
        prefix = f"baseline_accept[{index}]"
        detail = event.get("detail", _MISSING)
        if not isinstance(detail, dict):
            errors.append(f"{prefix}.detail must be an object")
            continue
        path = detail.get("path")
        path_error = _path_error(path)
        if path_error:
            errors.append(f"{prefix}.path {path_error}")
        alias_error = _hash_alias_error(detail)
        if alias_error:
            errors.append(f"{prefix}.{alias_error}")
        digest = _record_hash(detail)
        if digest is None:
            errors.append(
                f"{prefix}.hash must be a 64-character hexadecimal SHA-256")
        elif path_error is None and (path, digest) not in manifest_hashes:
            errors.append(
                f"{prefix} does not match a manifest preexisting_files path and hash")
        purpose = detail.get("purpose")
        if not isinstance(purpose, str) or not purpose.strip():
            errors.append(f"{prefix}.purpose must be a non-empty string")
        if type(detail.get("commit_allowed")) is not bool:
            errors.append(f"{prefix}.commit_allowed must be a boolean")
    return errors


def collect_accepted_preexisting_paths(manifest, events):
    """Return only fully authorized, manifest-backed baseline paths."""
    preexisting = manifest.get("preexisting_files") if isinstance(manifest, dict) else None
    if not isinstance(preexisting, list):
        return set()

    manifest_hashes = {}
    for record in preexisting:
        path = _canonical_path(record.get("path")) if isinstance(record, dict) else None
        digest = _record_hash(record)
        if path and digest:
            manifest_hashes.setdefault(path, set()).add(digest)

    accepted = set()
    for event in events or []:
        if not isinstance(event, dict) or event.get("event") != "baseline_accept":
            continue
        detail = event.get("detail")
        if not isinstance(detail, dict):
            continue
        path = _canonical_path(detail.get("path"))
        digest = _record_hash(detail)
        purpose = detail.get("purpose")
        commit_allowed = detail.get("commit_allowed")
        if (path and digest and isinstance(purpose, str) and purpose.strip()
                and type(commit_allowed) is bool
                and digest in manifest_hashes.get(path, set())):
            accepted.add(path)
    return accepted


def _validate_amendments(amendments):
    valid = []
    errors = []
    for index, amendment in enumerate(amendments or []):
        prefix = f"amendment[{index}]"
        if not isinstance(amendment, dict):
            errors.append(f"{prefix} must be an object")
            continue
        task_id_error = _task_id_error(amendment.get("task_id"))
        if task_id_error:
            errors.append(f"{prefix}.task_id {task_id_error}")
        planned_files, planned_errors = _validate_files(
            amendment.get("planned_files", _MISSING), f"{prefix}.planned_files")
        actual_files, actual_errors = _validate_files(
            amendment.get("actual_files", _MISSING), f"{prefix}.actual_files")
        errors.extend(planned_errors + actual_errors)
        reason = amendment.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{prefix}.reason must be a non-empty string")
        if (not task_id_error and not planned_errors and not actual_errors
                and isinstance(reason, str) and reason.strip()):
            valid.append({
                "task_id": amendment["task_id"],
                "planned_files": planned_files,
                "actual_files": actual_files,
                "reason": reason,
                "source": amendment,
            })
    return valid, errors


def _approved_amendment(task_id, planned_files, actual_files, amendments):
    for amendment in amendments:
        if (amendment["task_id"] == task_id
                and amendment["planned_files"] == planned_files
                and amendment["actual_files"] == actual_files):
            return amendment["source"]
    return None


def _changed_file(item):
    """Return a canonical baseline change path or an explicit validation error."""
    if isinstance(item, str):
        path = item
    elif isinstance(item, dict):
        has_path = "path" in item
        has_file = "file" in item
        if has_path and has_file and item["path"] != item["file"]:
            return None, "path and .file must match"
        path = item.get("path", item.get("file"))
    else:
        return None, "baseline change must be a path string or object"
    error = _path_error(path)
    return (path, error) if error else (path, None)


def check(p2_artifact, p4_artifacts, amendments, changed_files,
          accepted_preexisting_paths=None, evidence_errors=None):
    """Return deterministic P2/P4/diff trace evidence.

    Every path must already be a canonical project-relative POSIX path. Missing
    trace inputs or a missing baseline diff degrade to UNCHECKED; malformed input
    is a FAIL and is never normalized into an apparently matching file set.
    """
    result = {
        "verdict": "PASS",
        "task_drift": [],
        "unimplemented_tasks": [],
        "unclaimed_changes": [],
        "approved_amendments": [],
        "warnings": [],
        "schema_errors": [],
    }
    result["schema_errors"].extend(evidence_errors or [])
    accepted_paths = set()
    for index, path in enumerate(accepted_preexisting_paths or []):
        error = _path_error(path)
        if error:
            result["schema_errors"].append(
                f"accepted_preexisting_paths[{index}] {error}")
        else:
            accepted_paths.add(path)

    planned_tasks, p2_errors, has_dag = _collect_p2_tasks(p2_artifact)
    result["schema_errors"].extend(p2_errors)

    actual_by_id = {}
    has_p4_artifacts = isinstance(p4_artifacts, list) and bool(p4_artifacts)
    if not isinstance(p4_artifacts, list):
        result["schema_errors"].append("P4 artifacts must be a list")
    else:
        for index, artifact in enumerate(p4_artifacts):
            record, errors = _validate_p4_artifact(artifact, index)
            result["schema_errors"].extend(errors)
            if record:
                if record["task_id"] in actual_by_id:
                    result["schema_errors"].append(
                        f"duplicate P4 task_id: {record['task_id']}")
                    continue
                actual_by_id[record["task_id"]] = set(record["files"])
    actual_by_id = {task_id: sorted(files) for task_id, files in actual_by_id.items()}

    valid_amendments, amendment_errors = _validate_amendments(amendments)
    result["schema_errors"].extend(amendment_errors)

    baseline_paths = []
    if changed_files is None:
        result["warnings"].append("baseline diff is unavailable")
    elif not isinstance(changed_files, list):
        result["schema_errors"].append("baseline changes must be a list")
    else:
        for index, item in enumerate(changed_files):
            path, error = _changed_file(item)
            if error:
                result["schema_errors"].append(f"baseline change[{index}] {error}")
            else:
                baseline_paths.append(path)

    if not has_dag or not planned_tasks:
        if result["schema_errors"]:
            result["verdict"] = "FAIL"
        else:
            result["verdict"] = "UNCHECKED"
            result["warnings"].append("P2 artifact declares no dag_tasks")
        return result

    if not has_p4_artifacts:
        if result["schema_errors"]:
            result["verdict"] = "FAIL"
        else:
            result["verdict"] = "UNCHECKED"
            result["warnings"].append("run has no P4 task artifacts")
        return result

    planned_by_id = {task["task_id"]: task["files"] for task in planned_tasks}

    for task_id, planned_files in planned_by_id.items():
        if task_id not in actual_by_id:
            result["unimplemented_tasks"].append({
                "task_id": task_id,
                "planned_files": planned_files,
            })
            continue
        actual_files = actual_by_id[task_id]
        if actual_files == planned_files:
            continue
        amendment = _approved_amendment(task_id, planned_files, actual_files, valid_amendments)
        if amendment:
            result["approved_amendments"].append(amendment)
        else:
            result["task_drift"].append({
                "task_id": task_id,
                "planned_files": planned_files,
                "actual_files": actual_files,
            })

    for task_id, actual_files in actual_by_id.items():
        if task_id not in planned_by_id:
            result["task_drift"].append({
                "task_id": task_id,
                "planned_files": [],
                "actual_files": actual_files,
                "reason": "P4 artifact has no P2 DAG task",
            })

    claimed_files = {path for files in actual_by_id.values() for path in files}
    for path in baseline_paths:
        if (path in accepted_paths or path == ".cc-skill"
                or path.startswith(".cc-skill/")):
            continue
        if path not in claimed_files:
            result["unclaimed_changes"].append(path)
    result["unclaimed_changes"].sort()

    if (result["schema_errors"] or result["task_drift"]
            or result["unimplemented_tasks"] or result["unclaimed_changes"]):
        result["verdict"] = "FAIL"
    elif changed_files is None:
        result["verdict"] = "UNCHECKED"
    return result


def _read_events(run_dir):
    path = os.path.join(run_dir, "run.jsonl")
    if not os.path.isfile(path):
        return []
    events = []
    with open(path, encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid JSON in {path}:{line_number}: {error}") from error
    return events


def _read_manifest(run_dir):
    path = os.path.join(run_dir, "manifest.json")
    return load_json(path) if os.path.isfile(path) else {}


def _baseline_changes(root, baseline, preexisting_paths=None):
    if baseline is None:
        return None, []
    if baseline == "":
        raise ValueError("baseline must not be empty")
    if os.path.isfile(baseline):
        try:
            loaded = load_json(baseline)
        except json.JSONDecodeError:
            with open(baseline, encoding="utf-8") as source:
                return [line.rstrip("\r\n") for line in source if line.rstrip("\r\n")], []
        if isinstance(loaded, dict):
            if "changed_files" not in loaded:
                return None, ["baseline JSON object must contain changed_files"]
            if not isinstance(loaded["changed_files"], list):
                return None, ["baseline JSON changed_files must be a list"]
            return loaded["changed_files"], []
        if isinstance(loaded, list):
            return loaded, []
        raise ValueError(f"baseline file {baseline} must contain a list or changed_files")
    if os.path.isabs(baseline):
        raise ValueError(f"baseline file is not readable: {baseline}")
    completed = subprocess.run(
        ["git", "-C", root, "diff", "--name-only", "-z", baseline],
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        error = os.fsdecode(completed.stderr).strip()
        raise ValueError(error or f"cannot diff baseline {baseline}")
    tracked_changes = [
        os.fsdecode(path) for path in completed.stdout.split(b"\0") if path
    ]
    porcelain_paths = _git_porcelain_v1_paths(root)
    preexisting_paths = set(preexisting_paths or [])
    untracked = [
        path for status, path in porcelain_paths
        if (status == "??" and path
            and path not in preexisting_paths
            and path != ".cc-skill"
            and not path.startswith(".cc-skill/"))
    ]
    return sorted(set(tracked_changes).union(untracked)), []


def main(argv=None):
    parser = argparse.ArgumentParser(prog="design_trace.py")
    parser.add_argument("--p2", required=True, help="P2 design artifact JSON")
    parser.add_argument("--run-dir", required=True, help=".cc-skill run directory")
    parser.add_argument("--root", required=True, help="project root for baseline diff")
    parser.add_argument("--baseline", help="baseline commit or changed_files JSON/text file")
    parser.add_argument("--json", nargs="?", const="-", dest="json_out",
                        help="emit result JSON to stdout or an optional file")
    args = parser.parse_args(argv)

    try:
        if not os.path.isfile(args.p2):
            raise ValueError(f"P2 artifact is not a readable file: {args.p2}")
        if not os.path.isdir(args.root):
            raise ValueError(f"root is not a directory: {args.root}")
        if not os.path.isdir(args.run_dir):
            raise ValueError(f"run directory is not a directory: {args.run_dir}")
        p2 = load_json(args.p2)
        artifacts = collect_p4_artifacts(args.run_dir)
        events = _read_events(args.run_dir)
        manifest = _read_manifest(args.run_dir)
        amendments = collect_amendments(events)
        accepted_preexisting_paths = collect_accepted_preexisting_paths(manifest, events)
        preexisting_paths = {
            record.get("path") for record in manifest.get("preexisting_files", [])
            if isinstance(record, dict) and _canonical_path(record.get("path"))
        }
        changes, baseline_errors = _baseline_changes(
            args.root, args.baseline, preexisting_paths=preexisting_paths)
        result = check(
            p2, artifacts, amendments, changes,
            accepted_preexisting_paths=accepted_preexisting_paths,
            evidence_errors=_validate_preexisting_evidence(manifest, events) + baseline_errors,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"{parser.prog}: error: {error}", file=sys.stderr)
        return 2

    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.json_out:
        if args.json_out == "-":
            print(payload)
        else:
            try:
                with open(args.json_out, "w", encoding="utf-8") as output:
                    output.write(payload + "\n")
            except OSError as error:
                print(f"design_trace.py: error: cannot write JSON output: {error}", file=sys.stderr)
                return 2
    else:
        print(f"design_trace: {result['verdict']}")
    return 1 if result["verdict"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
