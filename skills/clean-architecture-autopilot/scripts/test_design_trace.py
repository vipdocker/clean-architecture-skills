#!/usr/bin/env python3
"""Regression tests for P2/P4/diff design trace reconciliation."""
import json
import os
import subprocess
import tempfile
import unittest

import design_trace


class DesignTraceTests(unittest.TestCase):
    def setUp(self):
        self.p2 = {
            "dag_tasks": [
                {"task_id": "T-Q2", "files_touched": ["old.py"]},
            ],
        }
        self.p4 = {"task_id": "T-Q2", "files": ["new.py"]}

    def test_unamended_task_file_drift_fails(self):
        result = design_trace.check(self.p2, [self.p4], [], changed_files=[])

        self.assertEqual("FAIL", result["verdict"])
        self.assertEqual("T-Q2", result["task_drift"][0]["task_id"])

    def test_matching_amendment_makes_task_trace_pass(self):
        amendments = [{
            "task_id": "T-Q2",
            "planned_files": ["old.py"],
            "actual_files": ["new.py"],
            "reason": "rename",
        }]

        result = design_trace.check(self.p2, [self.p4], amendments, changed_files=[])

        self.assertEqual("PASS", result["verdict"])
        self.assertEqual([amendments[0]], result["approved_amendments"])

    def test_missing_artifact_is_unchecked_not_pass(self):
        result = design_trace.check(self.p2, [], [], changed_files=[])

        self.assertEqual("UNCHECKED", result["verdict"])
        self.assertTrue(result["warnings"])

    def test_cli_rejects_present_malformed_p4_artifact(self):
        malformed_artifacts = [
            [],
            {},
            {"files": ["task.py"]},
            {"task_id": "T-1"},
        ]
        for artifact in malformed_artifacts:
            with self.subTest(artifact=artifact):
                with tempfile.TemporaryDirectory() as directory:
                    run_dir = os.path.join(directory, "run")
                    artifacts_dir = os.path.join(run_dir, "artifacts")
                    os.makedirs(artifacts_dir)
                    p2_path = os.path.join(directory, "p2.json")
                    baseline_path = os.path.join(directory, "baseline.json")
                    output_path = os.path.join(directory, "result.json")
                    with open(p2_path, "w", encoding="utf-8") as output:
                        json.dump({"dag_tasks": [
                            {"task_id": "T-1", "files": ["task.py"]},
                        ]}, output)
                    with open(os.path.join(artifacts_dir, "p4-task.json"), "w", encoding="utf-8") as output:
                        json.dump(artifact, output)
                    with open(baseline_path, "w", encoding="utf-8") as output:
                        json.dump({"changed_files": ["task.py"]}, output)

                    exit_code = design_trace.main([
                        "--p2", p2_path,
                        "--run-dir", run_dir,
                        "--root", directory,
                        "--baseline", baseline_path,
                        "--json", output_path,
                    ])

                    self.assertEqual(1, exit_code)
                    result = design_trace.load_json(output_path)
                    self.assertEqual("FAIL", result["verdict"])
                    self.assertTrue(result["schema_errors"])

    def test_empty_p4_still_rejects_malformed_amendment(self):
        malformed_amendment = {
            "task_id": "T-Q2",
            "planned_files": "old.py",
            "actual_files": ["new.py"],
            "reason": "rename",
        }

        result = design_trace.check(self.p2, [], [malformed_amendment], changed_files=[])

        self.assertEqual("FAIL", result["verdict"])
        self.assertIn(
            "amendment[0].planned_files must be a non-empty list[str]",
            result["schema_errors"],
        )

    def test_missing_dag_still_rejects_malformed_p4(self):
        result = design_trace.check({}, {"task_id": "T-Q2", "files": ["new.py"]}, [],
                                    changed_files=[])

        self.assertEqual("FAIL", result["verdict"])
        self.assertIn("P4 artifacts must be a list", result["schema_errors"])

    def test_missing_evidence_remains_unchecked_when_provided_inputs_are_valid(self):
        cases = [
            (self.p2, [], [{
                "task_id": "T-Q2",
                "planned_files": ["old.py"],
                "actual_files": ["new.py"],
                "reason": "rename",
            }]),
            ({}, [{"task_id": "T-Q2", "files": ["new.py"]}], []),
        ]

        for p2, p4, amendments in cases:
            with self.subTest(p2=p2, p4=p4, amendments=amendments):
                result = design_trace.check(p2, p4, amendments, changed_files=[])

                self.assertEqual("UNCHECKED", result["verdict"])
                self.assertTrue(result["warnings"])

    def test_rejects_present_non_list_dag_tasks_schema(self):
        for dag_tasks in ({}, "not-list", None):
            with self.subTest(dag_tasks=dag_tasks):
                result = design_trace.check(
                    {"dag_tasks": dag_tasks}, [], [], changed_files=[])

                self.assertEqual("FAIL", result["verdict"])
                self.assertIn("P2 dag_tasks must be a list", result["schema_errors"])

    def test_rejects_empty_dag_tasks_schema(self):
        result = design_trace.check({"dag_tasks": []}, [], [], changed_files=[])

        self.assertEqual("FAIL", result["verdict"])
        self.assertIn("P2 dag_tasks must not be empty", result["schema_errors"])

    def test_rejects_non_object_p2_artifact_schema(self):
        result = design_trace.check([], [], [], changed_files=[])

        self.assertEqual("FAIL", result["verdict"])
        self.assertIn("P2 artifact must be an object", result["schema_errors"])

    def test_rejects_absolute_parent_and_noncanonical_paths(self):
        p2 = {"dag_tasks": [{"task_id": "T-1", "files": ["/absolute.py"]}]}
        p4 = {"task_id": "T-1", "files": ["../outside.py"]}
        amendments = [{
            "task_id": "T-1",
            "planned_files": ["/absolute.py"],
            "actual_files": ["../outside.py"],
            "reason": "rename",
        }]

        result = design_trace.check(p2, [p4], amendments, changed_files=["src//bad.py"])

        self.assertEqual("FAIL", result["verdict"])
        self.assertTrue(result["schema_errors"])

    def test_rejects_invalid_or_missing_task_files_schema(self):
        p2 = {"dag_tasks": [{"task_id": "T-1", "files": "task.py"}]}
        p4 = {"task_id": "T-1"}

        result = design_trace.check(p2, [p4], [], changed_files=[])

        self.assertEqual("FAIL", result["verdict"])
        self.assertTrue(result["schema_errors"])

    def test_rejects_duplicate_p2_task_ids(self):
        p2 = {"dag_tasks": [
            {"task_id": "T-1", "files": ["first.py"]},
            {"task_id": "T-1", "files": ["second.py"]},
        ]}
        p4 = {"task_id": "T-1", "files": ["first.py"]}

        result = design_trace.check(p2, [p4], [], changed_files=[])

        self.assertEqual("FAIL", result["verdict"])
        self.assertIn("duplicate P2 task_id: T-1", result["schema_errors"])

    def test_rejects_duplicate_p4_task_ids(self):
        p2 = {"dag_tasks": [
            {"task_id": "T-1", "files": ["task.py"]},
        ]}
        p4 = [
            {"task_id": "T-1", "files": ["task.py"]},
            {"task_id": "T-1", "files": ["task.py"]},
        ]

        result = design_trace.check(p2, p4, [], changed_files=[])

        self.assertEqual("FAIL", result["verdict"])
        self.assertIn("duplicate P4 task_id: T-1", result["schema_errors"])

    def test_collect_p4_artifacts_ignores_non_p4_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts_dir = os.path.join(directory, "artifacts")
            os.makedirs(artifacts_dir)
            fixtures = {
                "p2-design.json": {"dag_tasks": []},
                "g3-review.json": {"task_id": "T-G3", "files": ["review.md"]},
                "g5-audit.json": {"task_id": "T-G5", "files": ["audit.md"]},
                "p4-task.json": {"task_id": "T-Q2", "files": ["new.py"]},
            }
            for name, artifact in fixtures.items():
                with open(os.path.join(artifacts_dir, name), "w", encoding="utf-8") as output:
                    json.dump(artifact, output)

            self.assertEqual(
                [fixtures["p4-task.json"]], design_trace.collect_p4_artifacts(directory))

    def test_preexisting_evidence_rejects_invalid_hashes_and_conflicting_paths(self):
        valid_hash = "a" * 64
        errors = design_trace._validate_preexisting_evidence(
            {"preexisting_files": [
                {"path": "invalid.py", "hash": "not-a-sha256"},
                {"path": "duplicate.py", "hash": valid_hash},
                {"path": "duplicate.py", "hash": "b" * 64},
            ]},
            [{"event": "baseline_accept", "detail": {
                "path": "accepted.py",
                "hash": "also-not-a-sha256",
                "purpose": "preexisting input",
                "commit_allowed": True,
            }}],
        )

        self.assertIn(
            "manifest preexisting_files[0].hash must be a 64-character hexadecimal SHA-256",
            errors,
        )
        self.assertIn("conflicting manifest preexisting_files path: duplicate.py", errors)
        self.assertIn(
            "baseline_accept[0].hash must be a 64-character hexadecimal SHA-256", errors)

    def test_baseline_accept_requires_purpose_and_boolean_commit_allowed(self):
        manifest = {"preexisting_files": [{"path": "input.py", "hash": "a" * 64}]}
        invalid_events = [
            {"event": "baseline_accept", "detail": {
                "path": "input.py", "hash": "digest", "commit_allowed": True,
            }},
            {"event": "baseline_accept", "detail": {
                "path": "input.py", "hash": "digest", "purpose": "input",
            }},
            {"event": "baseline_accept", "detail": {
                "path": "input.py", "hash": "digest", "purpose": "input",
                "commit_allowed": "true",
            }},
        ]

        self.assertEqual(
            set(), design_trace.collect_accepted_preexisting_paths(manifest, invalid_events))

    def test_cli_without_baseline_is_unchecked_not_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = os.path.join(directory, "run")
            artifacts_dir = os.path.join(run_dir, "artifacts")
            os.makedirs(artifacts_dir)
            p2_path = os.path.join(directory, "p2.json")
            output_path = os.path.join(directory, "result.json")
            with open(p2_path, "w", encoding="utf-8") as output:
                json.dump({"dag_tasks": [{"task_id": "T-1", "files": ["task.py"]}]}, output)
            with open(os.path.join(artifacts_dir, "p4-task.json"), "w", encoding="utf-8") as output:
                json.dump({"task_id": "T-1", "files": ["task.py"]}, output)

            exit_code = design_trace.main([
                "--p2", p2_path,
                "--run-dir", run_dir,
                "--root", directory,
                "--json", output_path,
            ])

            self.assertEqual(0, exit_code)
            result = design_trace.load_json(output_path)
            self.assertEqual("UNCHECKED", result["verdict"])
            self.assertTrue(result["warnings"])

    def test_cli_returns_exit_two_when_json_output_cannot_be_written(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = os.path.join(directory, "run")
            artifacts_dir = os.path.join(run_dir, "artifacts")
            os.makedirs(artifacts_dir)
            p2_path = os.path.join(directory, "p2.json")
            baseline_path = os.path.join(directory, "baseline.json")
            with open(p2_path, "w", encoding="utf-8") as output:
                json.dump({"dag_tasks": [{"task_id": "T-1", "files": ["task.py"]}]}, output)
            with open(os.path.join(artifacts_dir, "p4-task.json"), "w", encoding="utf-8") as output:
                json.dump({"task_id": "T-1", "files": ["task.py"]}, output)
            with open(baseline_path, "w", encoding="utf-8") as output:
                json.dump({"changed_files": ["task.py"]}, output)

            exit_code = design_trace.main([
                "--p2", p2_path,
                "--run-dir", run_dir,
                "--root", directory,
                "--baseline", baseline_path,
                "--json", directory,
            ])

            self.assertEqual(2, exit_code)

    def test_cli_reads_p4_artifact_amendment_and_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = os.path.join(directory, "run")
            artifacts_dir = os.path.join(run_dir, "artifacts")
            os.makedirs(artifacts_dir)
            p2_path = os.path.join(directory, "p2.json")
            baseline_path = os.path.join(directory, "baseline.json")
            output_path = os.path.join(directory, "result.json")
            with open(p2_path, "w", encoding="utf-8") as output:
                json.dump(self.p2, output)
            with open(os.path.join(artifacts_dir, "p4-task.json"), "w", encoding="utf-8") as output:
                json.dump(self.p4, output)
            with open(os.path.join(run_dir, "run.jsonl"), "w", encoding="utf-8") as output:
                output.write(json.dumps({
                    "event": "design_amendment",
                    "detail": {
                        "task_id": "T-Q2",
                        "planned_files": ["old.py"],
                        "actual_files": ["new.py"],
                        "reason": "rename",
                    },
                }) + "\n")
            with open(baseline_path, "w", encoding="utf-8") as output:
                json.dump({"changed_files": ["new.py"]}, output)

            exit_code = design_trace.main([
                "--p2", p2_path,
                "--run-dir", run_dir,
                "--root", directory,
                "--baseline", baseline_path,
                "--json", output_path,
            ])

            self.assertEqual(0, exit_code)
            self.assertEqual("PASS", design_trace.load_json(output_path)["verdict"])

    def test_cli_excludes_only_manifest_matched_accepted_preexisting_files(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = os.path.join(directory, "run")
            artifacts_dir = os.path.join(run_dir, "artifacts")
            os.makedirs(artifacts_dir)
            p2_path = os.path.join(directory, "p2.json")
            baseline_path = os.path.join(directory, "baseline.json")
            output_path = os.path.join(directory, "result.json")
            p2 = {"dag_tasks": [{"task_id": "T-1", "files": ["task.py"]}]}
            p4 = {"task_id": "T-1", "files": ["task.py"]}
            with open(p2_path, "w", encoding="utf-8") as output:
                json.dump(p2, output)
            with open(os.path.join(artifacts_dir, "p4-task.json"), "w", encoding="utf-8") as output:
                json.dump(p4, output)
            with open(os.path.join(run_dir, "manifest.json"), "w", encoding="utf-8") as output:
                json.dump({"preexisting_files": [
                    {"path": "accepted.py", "hash": "a" * 64},
                    {"path": "unaccepted.py", "hash": "b" * 64},
                ]}, output)
            with open(os.path.join(run_dir, "run.jsonl"), "w", encoding="utf-8") as output:
                output.write(json.dumps({
                    "event": "baseline_accept",
                    "detail": {
                        "path": "accepted.py",
                        "hash": "a" * 64,
                        "purpose": "preexisting input",
                        "commit_allowed": True,
                    },
                }) + "\n")
                output.write(json.dumps({
                    "event": "baseline_accept",
                    "detail": {
                        "path": "unaccepted.py",
                        "hash": "c" * 64,
                        "purpose": "preexisting input",
                        "commit_allowed": True,
                    },
                }) + "\n")
            with open(baseline_path, "w", encoding="utf-8") as output:
                json.dump({"changed_files": ["accepted.py", "unaccepted.py"]}, output)

            exit_code = design_trace.main([
                "--p2", p2_path,
                "--run-dir", run_dir,
                "--root", directory,
                "--baseline", baseline_path,
                "--json", output_path,
            ])

            self.assertEqual(1, exit_code)
            result = design_trace.load_json(output_path)
            self.assertNotIn("accepted.py", result["unclaimed_changes"])
            self.assertEqual(["unaccepted.py"], result["unclaimed_changes"])

    def test_cli_does_not_trust_baseline_accepted_preexisting_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = os.path.join(directory, "run")
            artifacts_dir = os.path.join(run_dir, "artifacts")
            os.makedirs(artifacts_dir)
            p2_path = os.path.join(directory, "p2.json")
            baseline_path = os.path.join(directory, "baseline.json")
            output_path = os.path.join(directory, "result.json")
            p2 = {"dag_tasks": [{"task_id": "T-1", "files": ["task.py"]}]}
            p4 = {"task_id": "T-1", "files": ["task.py"]}
            with open(p2_path, "w", encoding="utf-8") as output:
                json.dump(p2, output)
            with open(os.path.join(artifacts_dir, "p4-task.json"), "w", encoding="utf-8") as output:
                json.dump(p4, output)
            with open(os.path.join(run_dir, "manifest.json"), "w", encoding="utf-8") as output:
                json.dump({"preexisting_files": [
                    {"path": "unaccepted.py", "hash": "accepted-hash"},
                ]}, output)
            with open(os.path.join(run_dir, "run.jsonl"), "w", encoding="utf-8") as output:
                output.write(json.dumps({
                    "event": "baseline_accept",
                    "detail": {"path": "other.py", "hash": "other-hash"},
                }) + "\n")
            with open(baseline_path, "w", encoding="utf-8") as output:
                json.dump({"changed_files": [
                    {"path": "unaccepted.py", "accepted_preexisting": True},
                ]}, output)

            exit_code = design_trace.main([
                "--p2", p2_path,
                "--run-dir", run_dir,
                "--root", directory,
                "--baseline", baseline_path,
                "--json", output_path,
            ])

            self.assertEqual(1, exit_code)
            self.assertEqual(
                ["unaccepted.py"],
                design_trace.load_json(output_path)["unclaimed_changes"],
            )

    def test_cli_does_not_trust_baseline_preexisting_flag_with_wrong_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = os.path.join(directory, "run")
            artifacts_dir = os.path.join(run_dir, "artifacts")
            os.makedirs(artifacts_dir)
            p2_path = os.path.join(directory, "p2.json")
            baseline_path = os.path.join(directory, "baseline.json")
            output_path = os.path.join(directory, "result.json")
            p2 = {"dag_tasks": [{"task_id": "T-1", "files": ["task.py"]}]}
            p4 = {"task_id": "T-1", "files": ["task.py"]}
            with open(p2_path, "w", encoding="utf-8") as output:
                json.dump(p2, output)
            with open(os.path.join(artifacts_dir, "p4-task.json"), "w", encoding="utf-8") as output:
                json.dump(p4, output)
            with open(os.path.join(run_dir, "manifest.json"), "w", encoding="utf-8") as output:
                json.dump({"preexisting_files": [
                    {"path": "unaccepted.py", "hash": "accepted-hash"},
                ]}, output)
            with open(os.path.join(run_dir, "run.jsonl"), "w", encoding="utf-8") as output:
                output.write(json.dumps({
                    "event": "baseline_accept",
                    "detail": {"path": "unaccepted.py", "hash": "wrong-hash"},
                }) + "\n")
            with open(baseline_path, "w", encoding="utf-8") as output:
                json.dump({"changed_files": [
                    {"path": "unaccepted.py", "preexisting": True},
                ]}, output)

            exit_code = design_trace.main([
                "--p2", p2_path,
                "--run-dir", run_dir,
                "--root", directory,
                "--baseline", baseline_path,
                "--json", output_path,
            ])

            self.assertEqual(1, exit_code)
            self.assertEqual(
                ["unaccepted.py"],
                design_trace.load_json(output_path)["unclaimed_changes"],
            )

    def _write_valid_cli_inputs(self, directory):
        run_dir = os.path.join(directory, "run")
        artifacts_dir = os.path.join(run_dir, "artifacts")
        os.makedirs(artifacts_dir)
        p2_path = os.path.join(directory, "p2.json")
        baseline_path = os.path.join(directory, "baseline.json")
        with open(p2_path, "w", encoding="utf-8") as output:
            json.dump({"dag_tasks": [{"task_id": "T-1", "files": ["task.py"]}]}, output)
        with open(os.path.join(artifacts_dir, "p4-task.json"), "w", encoding="utf-8") as output:
            json.dump({"task_id": "T-1", "files": ["task.py"]}, output)
        with open(baseline_path, "w", encoding="utf-8") as output:
            json.dump({"changed_files": ["task.py"]}, output)
        return run_dir, p2_path, baseline_path

    def test_cli_rejects_every_malformed_design_amendment(self):
        malformed_amendments = [
            [],
            {"planned_files": ["old.py"], "actual_files": ["task.py"], "reason": "rename"},
            {"task_id": "T-1", "actual_files": ["task.py"], "reason": "rename"},
            {"task_id": "T-1", "planned_files": ["old.py"], "reason": "rename"},
            {"task_id": "T-1", "planned_files": ["old.py"], "actual_files": ["task.py"]},
            {"task_id": "T-1", "planned_files": "old.py", "actual_files": ["task.py"], "reason": "rename"},
            {"task_id": "T-1", "planned_files": ["old.py"], "actual_files": "task.py", "reason": "rename"},
            {"task_id": "T-1", "planned_files": ["../old.py"], "actual_files": ["task.py"], "reason": "rename"},
            {"task_id": "T-1", "planned_files": ["old.py"], "actual_files": ["../task.py"], "reason": "rename"},
        ]
        for amendment in malformed_amendments:
            with self.subTest(amendment=amendment):
                with tempfile.TemporaryDirectory() as directory:
                    run_dir, p2_path, baseline_path = self._write_valid_cli_inputs(directory)
                    output_path = os.path.join(directory, "result.json")
                    with open(os.path.join(run_dir, "run.jsonl"), "w", encoding="utf-8") as output:
                        output.write(json.dumps({
                            "event": "design_amendment", "detail": amendment,
                        }) + "\n")

                    exit_code = design_trace.main([
                        "--p2", p2_path,
                        "--run-dir", run_dir,
                        "--root", directory,
                        "--baseline", baseline_path,
                        "--json", output_path,
                    ])

                    self.assertEqual(1, exit_code)
                    self.assertEqual("FAIL", design_trace.load_json(output_path)["verdict"])

    def test_cli_rejects_malformed_preexisting_manifest_or_baseline_acceptance(self):
        cases = [
            ("manifest", {"preexisting_files": "input.py"}, []),
            ("manifest", {"preexisting_files": ["input.py"]}, []),
            ("manifest", {"preexisting_files": [{"hash": "digest"}]}, []),
            ("manifest", {"preexisting_files": [{"path": "input.py"}]}, []),
            ("manifest", {"preexisting_files": [{"path": "../input.py", "hash": "digest"}]}, []),
            ("event", {"preexisting_files": []}, [
                {"event": "baseline_accept", "detail": []},
            ]),
            ("event", {"preexisting_files": []}, [
                {"event": "baseline_accept", "detail": {"hash": "digest", "purpose": "input", "commit_allowed": True}},
            ]),
            ("event", {"preexisting_files": []}, [
                {"event": "baseline_accept", "detail": {"path": "input.py", "purpose": "input", "commit_allowed": True}},
            ]),
            ("event", {"preexisting_files": []}, [
                {"event": "baseline_accept", "detail": {"path": "input.py", "hash": "digest", "commit_allowed": True}},
            ]),
            ("event", {"preexisting_files": []}, [
                {"event": "baseline_accept", "detail": {"path": "../input.py", "hash": "digest", "purpose": "input", "commit_allowed": True}},
            ]),
            ("event", {"preexisting_files": []}, [
                {"event": "baseline_accept", "detail": {"path": "input.py", "hash": "digest", "purpose": "input", "commit_allowed": "true"}},
            ]),
        ]
        for kind, manifest, events in cases:
            with self.subTest(kind=kind, manifest=manifest, events=events):
                with tempfile.TemporaryDirectory() as directory:
                    run_dir, p2_path, baseline_path = self._write_valid_cli_inputs(directory)
                    output_path = os.path.join(directory, "result.json")
                    with open(os.path.join(run_dir, "manifest.json"), "w", encoding="utf-8") as output:
                        json.dump(manifest, output)
                    with open(os.path.join(run_dir, "run.jsonl"), "w", encoding="utf-8") as output:
                        for event in events:
                            output.write(json.dumps(event) + "\n")

                    exit_code = design_trace.main([
                        "--p2", p2_path,
                        "--run-dir", run_dir,
                        "--root", directory,
                        "--baseline", baseline_path,
                        "--json", output_path,
                    ])

                    self.assertEqual(1, exit_code)
                    self.assertEqual("FAIL", design_trace.load_json(output_path)["verdict"])

    def test_cli_rejects_baseline_json_without_list_changed_files(self):
        baselines = [{}, {"changed_files": "task.py"}]
        for baseline in baselines:
            with self.subTest(baseline=baseline):
                with tempfile.TemporaryDirectory() as directory:
                    run_dir, p2_path, baseline_path = self._write_valid_cli_inputs(directory)
                    output_path = os.path.join(directory, "result.json")
                    with open(baseline_path, "w", encoding="utf-8") as output:
                        json.dump(baseline, output)

                    exit_code = design_trace.main([
                        "--p2", p2_path,
                        "--run-dir", run_dir,
                        "--root", directory,
                        "--baseline", baseline_path,
                        "--json", output_path,
                    ])

                    self.assertEqual(1, exit_code)
                    self.assertEqual("FAIL", design_trace.load_json(output_path)["verdict"])

    def test_cli_returns_exit_two_for_unreadable_required_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir, p2_path, baseline_path = self._write_valid_cli_inputs(directory)
            cases = [
                ("--p2", os.path.join(directory, "missing-p2.json")),
                ("--run-dir", os.path.join(directory, "missing-run")),
                ("--root", os.path.join(directory, "missing-root")),
                ("--baseline", os.path.join(directory, "missing-baseline.json")),
            ]
            for option, unreadable_path in cases:
                with self.subTest(option=option):
                    args = [
                        "--p2", p2_path,
                        "--run-dir", run_dir,
                        "--root", directory,
                        "--baseline", baseline_path,
                    ]
                    index = args.index(option)
                    args[index + 1] = unreadable_path

                    self.assertEqual(2, design_trace.main(args))

    def test_cli_rejects_explicit_empty_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir, p2_path, _ = self._write_valid_cli_inputs(directory)

            exit_code = design_trace.main([
                "--p2", p2_path,
                "--run-dir", run_dir,
                "--root", directory,
                "--baseline", "",
            ])

            self.assertEqual(2, exit_code)

    def test_rejects_conflicting_hash_and_sha256_in_manifest_record(self):
        errors = design_trace._validate_preexisting_evidence(
            {"preexisting_files": [{
                "path": "input.py",
                "hash": "a" * 64,
                "sha256": "b" * 64,
            }]},
            [],
        )

        self.assertIn(
            "manifest preexisting_files[0].hash and .sha256 must match", errors)

    def test_rejects_unmatched_baseline_acceptance_without_changed_files(self):
        manifest = {"preexisting_files": [{"path": "input.py", "hash": "a" * 64}]}
        events = [{"event": "baseline_accept", "detail": {
            "path": "input.py",
            "hash": "b" * 64,
            "purpose": "preexisting input",
            "commit_allowed": True,
        }}]

        result = design_trace.check(
            {"dag_tasks": [{"task_id": "T-1", "files": ["task.py"]}]},
            [{"task_id": "T-1", "files": ["task.py"]}],
            [],
            changed_files=[],
            evidence_errors=design_trace._validate_preexisting_evidence(manifest, events),
        )

        self.assertEqual("FAIL", result["verdict"])
        self.assertIn(
            "baseline_accept[0] does not match a manifest preexisting_files path and hash",
            result["schema_errors"],
        )

    def test_p2_file_aliases_must_match_after_normalization(self):
        matching_p2 = {"dag_tasks": [{
            "task_id": "T-1",
            "files": ["task.py", "other.py", "task.py"],
            "files_touched": ["other.py", "task.py"],
        }]}
        matching_p4 = [{"task_id": "T-1", "files": ["task.py", "other.py"]}]

        self.assertEqual(
            "PASS", design_trace.check(matching_p2, matching_p4, [], changed_files=[])["verdict"])

        conflicting_p2 = {"dag_tasks": [{
            "task_id": "T-1",
            "files": ["task.py"],
            "files_touched": ["other.py"],
        }]}
        result = design_trace.check(conflicting_p2, matching_p4, [], changed_files=[])

        self.assertEqual("FAIL", result["verdict"])
        self.assertIn(
            "P2 dag_tasks[0].files and .files_touched must match",
            result["schema_errors"],
        )

    def test_baseline_path_and_file_aliases_must_match(self):
        p2 = {"dag_tasks": [{"task_id": "T-1", "files": ["task.py"]}]}
        p4 = [{"task_id": "T-1", "files": ["task.py"]}]

        matching = design_trace.check(
            p2, p4, [], changed_files=[{"path": "task.py", "file": "task.py"}])
        self.assertEqual("PASS", matching["verdict"])

        conflicting = design_trace.check(
            p2, p4, [], changed_files=[{"path": "task.py", "file": "other.py"}])
        self.assertEqual("FAIL", conflicting["verdict"])
        self.assertIn(
            "baseline change[0] path and .file must match",
            conflicting["schema_errors"],
        )

    def test_p4_tasks_wrapper_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts_dir = os.path.join(directory, "artifacts")
            os.makedirs(artifacts_dir)
            with open(os.path.join(artifacts_dir, "p4-wrapped.json"), "w", encoding="utf-8") as output:
                json.dump({"tasks": [{"task_id": "T-1", "files": ["task.py"]}]}, output)

            self.assertEqual(
                [{"task_id": "T-1", "files": ["task.py"]}],
                design_trace.collect_p4_artifacts(directory),
            )

    def test_p4_artifact_rejects_direct_record_with_tasks_wrapper(self):
        p2 = {"dag_tasks": [{"task_id": "T-1", "files": ["task.py"]}]}
        artifact = {
            "task_id": "T-1",
            "files": ["task.py"],
            "tasks": [{"task_id": "T-1", "files": ["task.py"]}],
        }

        with tempfile.TemporaryDirectory() as directory:
            artifacts_dir = os.path.join(directory, "artifacts")
            os.makedirs(artifacts_dir)
            with open(os.path.join(artifacts_dir, "p4-ambiguous.json"), "w", encoding="utf-8") as output:
                json.dump(artifact, output)

            result = design_trace.check(
                p2, design_trace.collect_p4_artifacts(directory), [], changed_files=[])

        self.assertEqual("FAIL", result["verdict"])
        self.assertIn(
            "P4 artifact[0] must not combine a direct record with a tasks wrapper",
            result["schema_errors"],
        )

    def test_p4_artifact_shape_rejects_wrapper_noise_and_malformed_siblings(self):
        malformed_artifacts = [
            [{"task_id": "T-1", "files": ["task.py"]}],
            {"tasks": [{"task_id": "T-1", "files": ["task.py"]}], "summary": "done"},
            {"tasks": [{"task_id": "T-1", "files": ["task.py"]}], "unknown": {}},
            {"tasks": [
                {"task_id": "T-1", "files": ["task.py"]},
                {"task_id": "T-2"},
            ]},
        ]
        p2 = {"dag_tasks": [{"task_id": "T-1", "files": ["task.py"]}]}

        for artifact in malformed_artifacts:
            with self.subTest(artifact=artifact):
                with tempfile.TemporaryDirectory() as directory:
                    artifacts_dir = os.path.join(directory, "artifacts")
                    os.makedirs(artifacts_dir)
                    with open(os.path.join(artifacts_dir, "p4-task.json"), "w", encoding="utf-8") as output:
                        json.dump(artifact, output)

                    result = design_trace.check(
                        p2, design_trace.collect_p4_artifacts(directory), [], changed_files=[])

                    self.assertEqual("FAIL", result["verdict"])
                    self.assertTrue(result["schema_errors"])

    def test_task_id_aliases_accept_official_p2_id_and_either_p4_alias(self):
        p2 = {"dag_tasks": [{"id": "T1", "files_touched": ["task.py"]}]}
        for p4 in (
            {"task_id": "T1", "files": ["task.py"]},
            {"id": "T1", "files": ["task.py"]},
        ):
            with self.subTest(p4=p4):
                result = design_trace.check(p2, [p4], [], changed_files=[])

                self.assertEqual("PASS", result["verdict"])

    def test_task_id_aliases_reject_conflicting_ids(self):
        conflicting_p2 = {
            "dag_tasks": [{
                "id": "T1", "task_id": "T2", "files_touched": ["task.py"],
            }]
        }
        conflicting_p4 = {
            "id": "T1", "task_id": "T2", "files": ["task.py"],
        }

        p2_result = design_trace.check(
            conflicting_p2, [{"task_id": "T1", "files": ["task.py"]}], [],
            changed_files=[])
        p4_result = design_trace.check(
            {"dag_tasks": [{"id": "T1", "files_touched": ["task.py"]}]},
            [conflicting_p4], [], changed_files=[])

        self.assertEqual("FAIL", p2_result["verdict"])
        self.assertIn("P2 dag_tasks[0].id and .task_id must match", p2_result["schema_errors"])
        self.assertEqual("FAIL", p4_result["verdict"])
        self.assertIn("P4 artifact[0].id and .task_id must match", p4_result["schema_errors"])

    def test_cli_checks_each_untracked_nested_git_file_against_p2_and_p4(self):
        nested_path = "docs/nested/extra.py"
        for declared in (False, True):
            with self.subTest(declared=declared), tempfile.TemporaryDirectory() as directory:
                root = os.path.join(directory, "repo")
                run_dir = os.path.join(directory, "run")
                artifacts_dir = os.path.join(run_dir, "artifacts")
                os.makedirs(root)
                os.makedirs(artifacts_dir)
                for command in (
                    ("init", "-q"),
                    ("config", "user.email", "test@example.com"),
                    ("config", "user.name", "Test User"),
                ):
                    subprocess.run(["git", "-C", root, *command], check=True)
                with open(os.path.join(root, "task.py"), "w", encoding="utf-8") as output:
                    output.write("value = 1\n")
                subprocess.run(["git", "-C", root, "add", "task.py"], check=True)
                subprocess.run(["git", "-C", root, "commit", "-qm", "baseline"], check=True)
                nested_file = os.path.join(root, *nested_path.split("/"))
                os.makedirs(os.path.dirname(nested_file))
                with open(nested_file, "w", encoding="utf-8") as output:
                    output.write("extra = 1\n")
                task_files = ["task.py", nested_path] if declared else ["task.py"]
                p2_path = os.path.join(directory, "p2.json")
                output_path = os.path.join(directory, "result.json")
                with open(p2_path, "w", encoding="utf-8") as output:
                    json.dump({"dag_tasks": [{"id": "T1", "files_touched": task_files}]}, output)
                with open(os.path.join(artifacts_dir, "p4-T1.json"), "w", encoding="utf-8") as output:
                    json.dump({"id": "T1", "files": task_files}, output)

                exit_code = design_trace.main([
                    "--p2", p2_path,
                    "--run-dir", run_dir,
                    "--root", root,
                    "--baseline", "HEAD",
                    "--json", output_path,
                ])

                result = design_trace.load_json(output_path)
                self.assertEqual(0 if declared else 1, exit_code)
                self.assertEqual("PASS" if declared else "FAIL", result["verdict"])
                self.assertEqual([] if declared else [nested_path], result["unclaimed_changes"])

    def test_cli_tracks_nul_porcelain_rename_and_special_untracked_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.join(directory, "repo")
            run_dir = os.path.join(directory, "run")
            artifacts_dir = os.path.join(run_dir, "artifacts")
            os.makedirs(root)
            os.makedirs(artifacts_dir)
            for command in (
                ("init", "-q"),
                ("config", "user.email", "test@example.com"),
                ("config", "user.name", "Test User"),
            ):
                subprocess.run(["git", "-C", root, *command], check=True)
            old_path = os.path.join(root, "rename old.txt")
            with open(old_path, "w", encoding="utf-8") as output:
                output.write("old\n")
            subprocess.run(["git", "-C", root, "add", "rename old.txt"], check=True)
            subprocess.run(["git", "-C", root, "commit", "-qm", "baseline"], check=True)
            subprocess.run(
                ["git", "-C", root, "mv", "rename old.txt", "renamed 当前.txt"], check=True)
            special_paths = ["a space.txt", "nested/目录/dirty file.txt"]
            for path in special_paths:
                full_path = os.path.join(root, *path.split("/"))
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as output:
                    output.write("dirty\n")
            task_paths = ["renamed 当前.txt", *special_paths]
            p2_path = os.path.join(directory, "p2.json")
            output_path = os.path.join(directory, "result.json")
            with open(p2_path, "w", encoding="utf-8") as output:
                json.dump({"dag_tasks": [{"id": "T1", "files_touched": task_paths}]}, output)
            with open(os.path.join(artifacts_dir, "p4-T1.json"), "w", encoding="utf-8") as output:
                json.dump({"id": "T1", "files": task_paths}, output)

            exit_code = design_trace.main([
                "--p2", p2_path,
                "--run-dir", run_dir,
                "--root", root,
                "--baseline", "HEAD",
                "--json", output_path,
            ])

            result = design_trace.load_json(output_path)
            self.assertEqual(0, exit_code)
            self.assertEqual("PASS", result["verdict"])
            self.assertEqual([], result["unclaimed_changes"])


if __name__ == "__main__":
    unittest.main()
