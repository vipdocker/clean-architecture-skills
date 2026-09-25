#!/usr/bin/env python3
"""Integration tests for cc_log's evidence-integrity event protocol."""
import datetime
import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("cc_log.py")
DEP_GRAPH_SPEC = importlib.util.spec_from_file_location(
    "dep_graph", SCRIPT.with_name("dep_graph.py")
)
dep_graph = importlib.util.module_from_spec(DEP_GRAPH_SPEC)
DEP_GRAPH_SPEC.loader.exec_module(dep_graph)


class CCLogProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-qm", "initial")
        (self.root / "input.md").write_text("initial input\n", encoding="utf-8")
        result = self._cc("init", "--slug", "evidence", "--title", "Evidence test")
        self.assertEqual(0, result.returncode, result.stderr)
        self.run_dir = Path(result.stdout.strip())

    def tearDown(self):
        self.temp_dir.cleanup()

    def _git(self, *args):
        subprocess.run(
            ["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True
        )

    def _cc(self, command, *args, detail=None):
        command_line = ["python3", str(SCRIPT), command, "--root", str(self.root)]
        if command != "init":
            command_line.extend(["--slug", "evidence"])
        command_line.extend(args)
        if detail is not None:
            command_line.extend(["--detail", json.dumps(detail)])
        return subprocess.run(command_line, check=False, capture_output=True, text=True)

    def _event(self, phase, event, detail, **kwargs):
        args = ["--phase", phase, "--event", event]
        for key, value in kwargs.items():
            args.extend([f"--{key.replace('_', '-')}", str(value)])
        return self._cc("event", *args, detail=detail)

    def _state(self):
        return json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))

    def _events(self):
        return [
            json.loads(line)
            for line in (self.run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()
        ]

    def _write_g5_artifact(self):
        artifact = self.run_dir / "artifacts" / "g5-review.json"
        artifact.write_text(
            json.dumps(
                {
                    "findings": [{"id": "F-1", "scope": "logger", "evidence": "test"}],
                    "verdict": "PASS_WITH_CONCERNS",
                    "review_mode": "full",
                }
            ),
            encoding="utf-8",
        )

    def _sign_off_debt(self, debt="F-1"):
        self._write_g5_artifact()
        self.assertEqual(
            0,
            self._event("G5", "phase_enter", {}).returncode,
        )
        self.assertEqual(
            0,
            self._event(
                "G5",
                "gate_verdict",
                {
                    "debts": [debt],
                    "trace_degraded_reason": "P2/P4 fixtures are intentionally omitted in this debt-chain test",
                },
                verdict="PASS_WITH_CONCERNS",
            ).returncode,
        )
        question = self._event(
            "G5",
            "user_loop",
            {"trigger": "debt_signoff", "debts": [debt], "questions": ["Accept debt?"]},
            status="awaiting_user",
        )
        self.assertEqual(0, question.returncode, question.stderr)
        question_seq = self._events()[-1]["seq"]
        signoff = self._event(
            "G5",
            "debt_signoff",
            {"answer": "I accept this debt", "user_loop_seq": question_seq, "debts": [debt]},
        )
        self.assertEqual(0, signoff.returncode, signoff.stderr)
        return question_seq, self._events()[-1]["seq"]

    def test_adopted_records_increment_ledger_with_basis(self):
        result = self._event(
            "P1",
            "phase_enter",
            {"adopted_without_asking": {"OQ-1": {"basis": "P0 codebase notes"}}},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        ledger = self._state()["question_ledger"]
        self.assertEqual(1, ledger["self_resolved"])
        self.assertEqual(
            [{"seq": 1, "question": "OQ-1", "basis": "P0 codebase notes"}],
            ledger["self_resolved_records"],
        )

    def test_reused_signoff_does_not_duplicate_debt_or_question(self):
        question_seq, signoff_seq = self._sign_off_debt()
        self._write_g5_artifact()
        self.assertEqual(
            0,
            self._event(
                "G5",
                "gate_verdict",
                {
                    "debts": ["F-1"],
                    "trace_degraded_reason": "P2/P4 fixtures are intentionally omitted in this debt-chain test",
                },
                verdict="PASS_WITH_CONCERNS",
            ).returncode,
        )
        before = self._state()

        result = self._event(
            "G5",
            "debt_signoff_reused",
            {
                "debts": ["F-1"],
                "user_loop_seq": question_seq,
                "debt_signoff_seq": signoff_seq,
            },
        )

        self.assertEqual(0, result.returncode, result.stderr)
        after = self._state()
        self.assertEqual([], after["debts"])
        self.assertEqual(before["question_ledger"]["asked"], after["question_ledger"]["asked"])
        self.assertEqual(before["debts_signed_off"], after["debts_signed_off"])

    def test_unmatched_reused_signoff_is_rejected(self):
        question_seq, signoff_seq = self._sign_off_debt()
        self._write_g5_artifact()
        self.assertEqual(
            0,
            self._event(
                "G5",
                "gate_verdict",
                {
                    "debts": ["F-2"],
                    "trace_degraded_reason": "P2/P4 fixtures are intentionally omitted in this debt-chain test",
                },
                verdict="PASS_WITH_CONCERNS",
            ).returncode,
        )

        result = self._event(
            "G5",
            "debt_signoff_reused",
            {
                "debts": ["F-2"],
                "user_loop_seq": question_seq,
                "debt_signoff_seq": signoff_seq,
                "answer": "I accept this debt",
            },
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("debt_signoff_reused", result.stderr)

    def test_work_lifecycle_and_design_amendment_are_tracked_per_activity(self):
        detail = {
            "activity_id": "T-1",
            "component": "orders",
            "logical_wave_id": "wave-1",
            "execution_batch_id": "wave-1-batch-1",
        }
        self.assertEqual(0, self._event("P4", "work_started", detail).returncode)
        self.assertEqual(0, self._event("P4", "work_finished", detail).returncode)
        amendment = {
            "task_id": "T-1",
            "planned_files": ["old.py"],
            "actual_files": ["new.py"],
            "reason": "renamed boundary module",
        }
        self.assertEqual(0, self._event("P4", "design_amendment", amendment).returncode)

        state = self._state()
        self.assertEqual("finished", state["activity_lifecycle"]["T-1"]["status"])
        self.assertEqual([amendment], state["design_amendments"])

    def test_baseline_accept_only_allows_init_dirty_path_and_hash(self):
        manifest = json.loads((self.run_dir / "manifest.json").read_text(encoding="utf-8"))
        preexisting = manifest["preexisting_files"]
        self.assertEqual("input.md", preexisting[0]["path"])
        digest = preexisting[0]["sha256"]
        accepted = self._event(
            "P0",
            "baseline_accept",
            {"path": "input.md", "sha256": digest, "purpose": "provided input", "commit_allowed": True},
        )
        self.assertEqual(0, accepted.returncode, accepted.stderr)
        self.assertEqual(["input.md"], [r["path"] for r in self._state()["baseline_acceptances"]])

        (self.root / "later.md").write_text("later\n", encoding="utf-8")
        rejected = self._event(
            "P0",
            "baseline_accept",
            {
                "path": "later.md",
                "sha256": hashlib.sha256(b"later\n").hexdigest(),
                "purpose": "not initial",
                "commit_allowed": True,
            },
        )
        self.assertEqual(2, rejected.returncode)

    def test_verification_record_requires_counts_and_warning_reasons(self):
        valid = {
            "tool": "ruff",
            "status": "PASS_WITH_ACCEPTED_WARNINGS",
            "artifact": "artifacts/ruff.txt",
            "counts": {"error": 0, "warning": 1, "info": 0},
            "accepted_warnings": [
                {"rule": "E501", "path": "cc_log.py", "reason": "legacy generated prose"}
            ],
        }
        accepted = self._event("P4", "verification_recorded", valid)
        self.assertEqual(0, accepted.returncode, accepted.stderr)
        self.assertEqual([valid], self._state()["verification_records"])

        invalid = dict(valid)
        invalid["accepted_warnings"] = [{"rule": "E501", "path": "cc_log.py", "reason": ""}]
        rejected = self._event("P4", "verification_recorded", invalid)
        self.assertEqual(2, rejected.returncode)
        self.assertIn("accepted_warnings", rejected.stderr)

    def test_work_finished_must_match_started_phase_and_p4_metadata(self):
        def assert_rejected(start_phase, start_detail, finish_phase, finish_detail):
            started = self._event(start_phase, "work_started", start_detail)
            self.assertEqual(0, started.returncode, started.stderr)
            finished = self._event(finish_phase, "work_finished", finish_detail)
            self.assertEqual(2, finished.returncode, finished.stderr)
            self.assertIn("work_finished", finished.stderr)

        p4_detail = {
            "activity_id": "p4-cross-phase",
            "component": "orders",
            "logical_wave_id": "wave-1",
            "execution_batch_id": "wave-1-batch-1",
        }
        assert_rejected(
            "P3",
            {"activity_id": "p3-cross-phase"},
            "P4",
            {**p4_detail, "activity_id": "p3-cross-phase"},
        )
        assert_rejected(
            "P4",
            p4_detail,
            "P3",
            {"activity_id": "p4-cross-phase"},
        )
        for field, value in (
            ("component", "billing"),
            ("logical_wave_id", "wave-2"),
            ("execution_batch_id", "wave-1-batch-2"),
        ):
            start = {**p4_detail, "activity_id": f"p4-{field}"}
            finish = {**start, field: value}
            assert_rejected("P4", start, "P4", finish)

        missing_metadata = {**p4_detail, "activity_id": "p4-missing"}
        del missing_metadata["execution_batch_id"]
        assert_rejected(
            "P4",
            {**p4_detail, "activity_id": "p4-missing"},
            "P4",
            missing_metadata,
        )

    def test_reused_signoff_rejects_force_formed_source_chain(self):
        self._write_g5_artifact()
        forced_verdict = self._cc(
            "event",
            "--phase",
            "G5",
            "--event",
            "gate_verdict",
            "--verdict",
            "PASS_WITH_CONCERNS",
            "--force",
            detail={
                "debts": ["F-1"],
                "trace_degraded_reason": "P2/P4 fixtures are intentionally omitted in this force-chain test",
            },
        )
        self.assertEqual(0, forced_verdict.returncode, forced_verdict.stderr)
        verdict_seq = self._events()[-1]["seq"]
        state = self._state()
        state["debts"] = ["F-1"]
        (self.run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        question = self._event(
            "G5",
            "user_loop",
            {"trigger": "debt_signoff", "debts": ["F-1"], "questions": ["Accept debt?"]},
            status="awaiting_user",
        )
        self.assertEqual(0, question.returncode, question.stderr)
        question_seq = self._events()[-1]["seq"]
        signoff = self._event(
            "G5",
            "debt_signoff",
            {"answer": "I accept this debt", "user_loop_seq": question_seq, "debts": ["F-1"]},
        )
        self.assertEqual(0, signoff.returncode, signoff.stderr)
        signoff_seq = self._events()[-1]["seq"]
        self.assertLess(verdict_seq, question_seq)
        self.assertLess(question_seq, signoff_seq)

        state = self._state()
        state["debts"] = ["F-1"]
        (self.run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        reused = self._event(
            "G5",
            "debt_signoff_reused",
            {"debts": ["F-1"], "user_loop_seq": question_seq, "debt_signoff_seq": signoff_seq},
        )
        self.assertEqual(2, reused.returncode)
        self.assertIn("debt_signoff_reused", reused.stderr)

    def test_verification_record_enforces_warning_and_error_status_contracts(self):
        base = {
            "tool": "ruff",
            "status": "PASS",
            "artifact": "artifacts/ruff.txt",
            "counts": {"error": 0, "warning": 1, "info": 0},
        }
        for detail in (
            base,
            {**base, "status": "PASS_WITH_ACCEPTED_WARNINGS", "accepted_warnings": []},
            {
                **base,
                "accepted_warnings": [
                    {"rule": "E501", "path": "cc_log.py", "reason": ""}
                ],
            },
            {
                **base,
                "counts": {"error": 0, "warning": 2, "info": 0},
                "accepted_warnings": [
                    {"rule": "E501", "path": "cc_log.py", "reason": "one warning only"}
                ],
            },
            {
                **base,
                "counts": {"error": 0, "warning": 0, "info": 0},
                "accepted_warnings": [
                    {"rule": "E501", "path": "cc_log.py", "reason": "not allowed"}
                ],
            },
            {**base, "counts": {"error": 1, "warning": 0, "info": 0}},
        ):
            with self.subTest(detail=detail):
                result = self._event("P4", "verification_recorded", detail)
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertIn("verification_recorded", result.stderr)

    def test_init_records_renamed_dirty_file_at_current_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            def git(*args):
                return subprocess.run(
                    ["git", "-C", str(root), *args],
                    check=True,
                    capture_output=True,
                    text=True,
                )

            git("init", "-q")
            git("config", "user.email", "test@example.com")
            git("config", "user.name", "Test User")
            (root / "old.py").write_text("old contents\n", encoding="utf-8")
            git("add", "old.py")
            git("commit", "-qm", "initial")
            git("mv", "old.py", "new.py")

            initialized = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "init",
                    "--root",
                    str(root),
                    "--slug",
                    "rename",
                    "--title",
                    "Rename provenance",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, initialized.returncode, initialized.stderr)
            run_dir = Path(initialized.stdout.strip())
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            renamed = next(record for record in manifest["preexisting_files"] if record["path"] == "new.py")
            digest = hashlib.sha256(b"old contents\n").hexdigest()
            self.assertEqual("R ", renamed["status"])
            self.assertEqual(digest, renamed["sha256"])

            accepted = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "event",
                    "--root",
                    str(root),
                    "--slug",
                    "rename",
                    "--phase",
                    "P0",
                    "--event",
                    "baseline_accept",
                    "--detail",
                    json.dumps(
                        {
                            "path": "new.py",
                            "sha256": digest,
                            "purpose": "pre-existing rename",
                            "commit_allowed": True,
                        }
                    ),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, accepted.returncode, accepted.stderr)

    def test_nul_porcelain_records_current_and_special_dirty_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            def git(*args):
                return subprocess.run(
                    ["git", "-C", str(root), *args],
                    check=True,
                    capture_output=True,
                    text=True,
                )

            git("init", "-q")
            git("config", "user.email", "test@example.com")
            git("config", "user.name", "Test User")
            (root / "rename old.txt").write_text("old\n", encoding="utf-8")
            git("add", "rename old.txt")
            git("commit", "-qm", "initial")
            git("mv", "rename old.txt", "renamed 当前.txt")
            (root / "a space.txt").write_text("space\n", encoding="utf-8")
            nested = root / "nested" / "目录" / "dirty file.txt"
            nested.parent.mkdir(parents=True)
            nested.write_text("unicode\n", encoding="utf-8")

            initialized = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "init",
                    "--root",
                    str(root),
                    "--slug",
                    "porcelain",
                    "--title",
                    "Porcelain paths",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, initialized.returncode, initialized.stderr)
            run_dir = Path(initialized.stdout.strip())
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            preexisting = {record["path"]: record for record in manifest["preexisting_files"]}
            self.assertIn("renamed 当前.txt", preexisting)
            self.assertEqual("R ", preexisting["renamed 当前.txt"]["status"])
            self.assertIn("a space.txt", preexisting)
            self.assertIn("nested/目录/dirty file.txt", preexisting)
            self.assertNotIn('"a space.txt"', preexisting)

            (root / "post space.txt").write_text("post\n", encoding="utf-8")
            post_nested = root / "nested" / "后" / "new file.txt"
            post_nested.parent.mkdir(parents=True)
            post_nested.write_text("post unicode\n", encoding="utf-8")
            report = subprocess.run(
                ["python3", str(SCRIPT), "report", "--root", str(root), "--slug", "porcelain"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, report.returncode, report.stderr)
            text = (run_dir / "report.md").read_text(encoding="utf-8")
            self.assertIn("`post space.txt`", text)
            self.assertIn("`nested/后/new file.txt`", text)


class CCLogGateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-qm", "initial")
        result = self._cc("init", "--slug", "gates", "--title", "Gate tests")
        self.assertEqual(0, result.returncode, result.stderr)
        self.run_dir = Path(result.stdout.strip())

    def tearDown(self):
        self.temp_dir.cleanup()

    def _git(self, *args):
        subprocess.run(
            ["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True
        )

    def _cc(self, command, *args, detail=None):
        command_line = ["python3", str(SCRIPT), command, "--root", str(self.root)]
        if command != "init":
            command_line.extend(["--slug", "gates"])
        command_line.extend(args)
        if detail is not None:
            command_line.extend(["--detail", json.dumps(detail)])
        return subprocess.run(command_line, check=False, capture_output=True, text=True)

    def _event(self, phase, event, detail=None, **kwargs):
        args = ["--phase", phase, "--event", event]
        for key, value in kwargs.items():
            args.extend([f"--{key.replace('_', '-')}", str(value)])
        return self._cc("event", *args, detail=detail)

    def _write_artifact(self, name, value):
        (self.run_dir / "artifacts" / name).write_text(
            json.dumps(value), encoding="utf-8"
        )

    def _write_valid_g5_artifact(self):
        self._write_artifact(
            "g5-review.json",
            {
                "findings": [{"id": "G5-1", "scope": "logger", "evidence": "fixture"}],
                "verdict": "PASS",
                "review_mode": "full",
            },
        )

    def test_passing_g5_rejects_failed_design_trace(self):
        self._write_artifact(
            "p2-design.json",
            {"dag_tasks": [{"task_id": "T1", "files_touched": ["tracked.txt"]}]},
        )
        self._write_artifact(
            "p4-T1.json",
            {"task_id": "T1", "files": ["different.txt"]},
        )
        self._write_valid_g5_artifact()
        self.assertEqual(0, self._event("G5", "phase_enter", {}).returncode)

        result = self._event("G5", "gate_verdict", {}, verdict="PASS")

        self.assertEqual(2, result.returncode)
        self.assertIn("design trace", result.stderr)

    def test_g3_rejects_missing_component_graph_evidence_when_map_declared(self):
        self._write_artifact(
            "p2-design.json",
            {
                "component_map": {
                    "ownership": [
                        {"module": "app.domain", "component": "orders", "kind": "domain"}
                    ]
                }
            },
        )
        self._write_artifact("g3-audit.json", {"verdict": "APPROVED", "findings": []})
        self.assertEqual(0, self._event("G3", "phase_enter", {}).returncode)

        result = self._event("G3", "gate_verdict", {}, verdict="APPROVED")

        self.assertEqual(2, result.returncode)
        self.assertIn("component_graph", result.stderr)

    def test_g3_records_valid_component_graph_degradation_when_ownership_exists(self):
        degraded_reason = {
            "reason": "The repository is a documentation-only fixture with no importable modules to scan.",
            "scan_attempted": True,
            "scope": ["docs", "fixtures"],
        }
        self._write_artifact(
            "p2-design.json",
            {
                "component_map": {
                    "ownership": [
                        {"module": "app.domain", "component": "orders", "kind": "domain"}
                    ]
                }
            },
        )
        self._write_artifact(
            "g3-audit.json",
            {"component_graph_degraded_reason": degraded_reason},
        )
        self.assertEqual(0, self._event("G3", "phase_enter", {}).returncode)

        result = self._event("G3", "gate_verdict", {}, verdict="APPROVED")

        self.assertEqual(0, result.returncode, result.stderr)
        state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(degraded_reason, state["component_graph_degraded_reason"])
        self.assertEqual(
            degraded_reason,
            self._events()[-1]["detail"]["component_graph_degraded_reason"],
        )

    def test_g3_rejects_invalid_or_ambiguous_component_graph_degradation(self):
        self._write_artifact(
            "p2-design.json",
            {
                "component_map": {
                    "ownership": [
                        {"module": "app.domain", "component": "orders", "kind": "domain"}
                    ]
                }
            },
        )
        valid_graph = {
            "component_edges": [],
            "component_sccs": [],
            "unowned_modules": [],
            "ownership_errors": [],
        }
        invalid_reasons = (
            None,
            {},
            {"reason": "", "scan_attempted": True, "scope": ["app"]},
            {"reason": "scan unavailable", "scan_attempted": "true", "scope": ["app"]},
            {"reason": "scan unavailable", "scan_attempted": True, "scope": []},
            {"reason": "scan unavailable", "scan_attempted": True, "scope": ["app", ""]},
            {
                "reason": "scan unavailable",
                "scan_attempted": True,
                "scope": ["app"],
                "unexpected": "extra key",
            },
        )
        self.assertEqual(0, self._event("G3", "phase_enter", {}).returncode)
        for reason in invalid_reasons:
            with self.subTest(reason=reason):
                self._write_artifact(
                    "g3-audit.json",
                    {"component_graph_degraded_reason": reason},
                )
                result = self._event("G3", "gate_verdict", {}, verdict="APPROVED")
                self.assertEqual(2, result.returncode)
                self.assertIn("component_graph_degraded_reason", result.stderr)

        self._write_artifact(
            "g3-audit.json",
            {
                "component_graph": valid_graph,
                "component_graph_degraded_reason": {
                    "reason": "scan unavailable", "scan_attempted": True, "scope": ["app"],
                },
            },
        )
        result = self._event("G3", "gate_verdict", {}, verdict="APPROVED")
        self.assertEqual(2, result.returncode)
        self.assertIn("must not combine", result.stderr)

    def test_g3_rejects_component_scc_without_specific_legacy_exception(self):
        self._write_artifact(
            "p2-design.json",
            {
                "component_map": {
                    "ownership": [
                        {"module": "app.domain", "component": "orders", "kind": "domain"}
                    ]
                }
            },
        )
        self._write_artifact(
            "g3-audit.json",
            {
                "component_graph": {
                    "component_edges": [{"from": "orders", "to": "billing"}],
                    "component_sccs": [["billing", "orders"]],
                    "unowned_modules": [],
                    "ownership_errors": [],
                }
            },
        )
        self.assertEqual(0, self._event("G3", "phase_enter", {}).returncode)

        result = self._event("G3", "gate_verdict", {}, verdict="APPROVED")

        self.assertEqual(2, result.returncode)
        self.assertIn("component_sccs", result.stderr)

    def test_g3_rejects_malformed_component_graph_and_legacy_exceptions(self):
        self._write_artifact(
            "p2-design.json",
            {
                "component_map": {
                    "ownership": [
                        {"module": "app.domain", "component": "orders", "kind": "domain"}
                    ]
                }
            },
        )
        self.assertEqual(0, self._event("G3", "phase_enter", {}).returncode)
        valid_graph = {
            "component_edges": [],
            "component_sccs": [],
            "unowned_modules": [],
            "ownership_errors": [],
        }
        malformed_graphs = (
            {**valid_graph, "component_edges": [{"from": "orders", "to": ""}]},
            {**valid_graph, "component_sccs": [["orders"]]},
            {**valid_graph, "unowned_modules": [None]},
            {**valid_graph, "ownership_errors": [{"message": ""}]},
            {
                **valid_graph,
                "ownership_errors": [
                    {"message": "app.domain lacks an owner", "error": "legacy"}
                ],
            },
        )
        malformed_legacy_exceptions = (
            None,
            [None],
            [
                {
                    "category": "unowned_modules",
                    "legacy_id": "LEGACY-1",
                    "reason": "Specific approved legacy boundary remains until migration.",
                    "subjects": None,
                }
            ],
        )

        for graph in malformed_graphs:
            with self.subTest(component_graph=graph):
                self._write_artifact("g3-audit.json", {"component_graph": graph})
                result = self._event("G3", "gate_verdict", {}, verdict="APPROVED")
                self.assertEqual(2, result.returncode)
                self.assertNotIn("Traceback", result.stderr)

        for legacy_exceptions in malformed_legacy_exceptions:
            with self.subTest(legacy_exceptions=legacy_exceptions):
                self._write_artifact(
                    "g3-audit.json",
                    {
                        "component_graph": valid_graph,
                        "legacy_exceptions": legacy_exceptions,
                    },
                )
                result = self._event("G3", "gate_verdict", {}, verdict="APPROVED")
                self.assertEqual(2, result.returncode)
                self.assertNotIn("Traceback", result.stderr)

    def test_g3_accepts_message_ownership_errors_with_specific_legacy_exception(self):
        ownership_error = {"message": "app.domain lacks a declared component owner"}
        self._write_artifact(
            "p2-design.json",
            {
                "component_map": {
                    "ownership": [
                        {"module": "app.domain", "component": "orders", "kind": "domain"}
                    ]
                }
            },
        )
        self._write_artifact(
            "g3-audit.json",
            {
                "component_graph": {
                    "component_edges": [],
                    "component_sccs": [],
                    "unowned_modules": [],
                    "ownership_errors": [ownership_error],
                },
                "legacy_exceptions": [
                    {
                        "category": "ownership_errors",
                        "legacy_id": "LEGACY-1",
                        "reason": "Approved migration keeps this ownership gap temporary.",
                        "subjects": [ownership_error],
                    }
                ],
            },
        )
        self.assertEqual(0, self._event("G3", "phase_enter", {}).returncode)

        result = self._event("G3", "gate_verdict", {}, verdict="APPROVED")

        self.assertEqual(0, result.returncode, result.stderr)

    def test_g3_accepts_dep_graph_message_ownership_error_with_legacy_exception(self):
        package = self.root / "app"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "domain.py").write_text("", encoding="utf-8")
        p2 = {
            "component_map": {
                "ownership": [
                    {"module": "app", "component": "package", "kind": "framework"},
                    {"module": "app.domain", "component": "orders", "kind": "domain"},
                    {"module": "app.unknown", "component": "unused", "kind": "framework"},
                ]
            }
        }
        self._write_artifact("p2-design.json", p2)
        audit_path = self.run_dir / "artifacts" / "g3-audit.json"
        produced = subprocess.run(
            [
                "python3",
                str(SCRIPT.with_name("dep_graph.py")),
                "--root",
                str(self.root),
                "--scan-dirs",
                "app",
                "--component-map",
                str(self.run_dir / "artifacts" / "p2-design.json"),
                "--json",
                str(audit_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(1, produced.returncode, produced.stderr)
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        ownership_errors = audit["component_graph"]["ownership_errors"]
        self.assertEqual(1, len(ownership_errors))
        self.assertEqual({"message"}, set(ownership_errors[0]))
        self.assertTrue(ownership_errors[0]["message"])
        audit["legacy_exceptions"] = [
            {
                "category": "ownership_errors",
                "legacy_id": "LEGACY-DEP-GRAPH-1",
                "reason": "Approved fixture exception preserves this known graph ownership error.",
                "subjects": ownership_errors,
            }
        ]
        self._write_artifact("g3-audit.json", audit)
        self.assertEqual(0, self._event("G3", "phase_enter", {}).returncode)

        result = self._event("G3", "gate_verdict", {}, verdict="APPROVED")

        self.assertEqual(0, result.returncode, result.stderr)

    def test_passing_g5_requires_reason_for_unchecked_trace(self):
        self._write_valid_g5_artifact()
        self.assertEqual(0, self._event("G5", "phase_enter", {}).returncode)

        result = self._event("G5", "gate_verdict", {}, verdict="PASS")

        self.assertEqual(2, result.returncode)
        self.assertIn("trace_degraded_reason", result.stderr)

    def test_open_gate_activity_prevents_idle_pause_credit(self):
        self.assertEqual(
            0,
            self._event("G5", "work_started", {"activity_id": "g5-full-review"}).returncode,
        )
        log_path = self.run_dir / "run.jsonl"
        events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        events[-1]["ts"] = (
            datetime.datetime.now().astimezone() - datetime.timedelta(minutes=31)
        ).isoformat()
        log_path.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
        )

        self.assertEqual(0, self._event("G5", "audit_progress", {}).returncode)

        pause = next(event for event in self._events() if event["event"] == "pause")
        self.assertFalse(pause["detail"]["credited_to_idle"])
        self.assertEqual(["g5-full-review"], pause["detail"]["activities_in_flight"])

    def test_passing_verification_with_warnings_requires_acceptance_reason(self):
        result = self._event(
            "P4",
            "verification_recorded",
            {
                "tool": "ruff",
                "status": "PASS_WITH_ACCEPTED_WARNINGS",
                "artifact": "artifacts/ruff.txt",
                "counts": {"error": 0, "warning": 1, "info": 0},
                "accepted_warnings": [{"rule": "E501", "path": "cc_log.py", "reason": ""}],
            },
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("accepted_warnings", result.stderr)

    def _events(self):
        return [
            json.loads(line)
            for line in (self.run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()
        ]


class CCLogReportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-qm", "initial")
        (self.root / "input.md").write_text("provided before init\n", encoding="utf-8")
        result = self._cc("init", "--slug", "report", "--title", "Report tests")
        self.assertEqual(0, result.returncode, result.stderr)
        self.run_dir = Path(result.stdout.strip())

    def tearDown(self):
        self.temp_dir.cleanup()

    def _git(self, *args):
        subprocess.run(
            ["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True
        )

    def _cc(self, command, *args, detail=None):
        command_line = ["python3", str(SCRIPT), command, "--root", str(self.root)]
        if command != "init":
            command_line.extend(["--slug", "report"])
        command_line.extend(args)
        if detail is not None:
            command_line.extend(["--detail", json.dumps(detail)])
        return subprocess.run(command_line, check=False, capture_output=True, text=True)

    def _event(self, phase, event, detail=None, **kwargs):
        args = ["--phase", phase, "--event", event]
        for key, value in kwargs.items():
            args.extend([f"--{key.replace('_', '-')}", str(value)])
        result = self._cc("event", *args, detail=detail)
        self.assertEqual(0, result.returncode, result.stderr)

    def _prepare_p4_component(self, with_lifecycle, p2_design=None):
        (self.run_dir / "artifacts" / "p2-design.json").write_text(
            json.dumps(p2_design or {}), encoding="utf-8"
        )
        self._event("G3", "phase_enter", {})
        self._event("G3", "gate_verdict", {}, verdict="APPROVED")
        self._event("P4", "phase_enter", {})
        lifecycle = {
            "activity_id": "orders-implementation",
            "component": "orders",
            "logical_wave_id": "wave-1",
            "execution_batch_id": "batch-1",
        }
        if with_lifecycle:
            self._event("P4", "work_started", lifecycle)
        self._event(
            "P4",
            "agent_dispatch",
            {"component": "orders"},
            agent="ca-clean-implementer",
            skills="test-driven-development",
        )
        self._event("P4", "component_done", {"component": "orders", "status": "DONE"})
        if with_lifecycle:
            self._event("P4", "work_finished", lifecycle)
        self._event("P4", "phase_exit", {})
        self._normalize_event_times()

    def _normalize_event_times(self):
        event_path = self.run_dir / "run.jsonl"
        events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
        start = datetime.datetime(2026, 9, 25, 9, 0, tzinfo=datetime.timezone.utc)
        for index, event in enumerate(events):
            event["ts"] = (start + datetime.timedelta(minutes=index)).isoformat()
        event_path.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
        )

    def _report_text(self):
        result = self._cc("report")
        self.assertEqual(0, result.returncode, result.stderr)
        return (self.run_dir / "report.md").read_text(encoding="utf-8")

    def _state(self):
        return json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))

    def test_batched_run_omits_parallelism_and_component_duration_claims(self):
        self._prepare_p4_component(with_lifecycle=False)

        report = self._report_text()

        self.assertIn("timestamp_granularity=batched", report)
        self.assertIn("不可归因", report)
        self.assertNotIn("P4 并行度", report)
        self.assertNotIn("模型 ROI", report)
        self.assertNotIn("模型路由 ROI（观测）", report)
        self.assertNotIn("lifecycle-supported duration", report)

    def test_lifecycle_run_reports_observed_model_routing_roi(self):
        self._prepare_p4_component(
            with_lifecycle=True,
            p2_design={
                "dag_tasks": [
                    {"id": "orders", "recommended_model": "premium"},
                ]
            },
        )

        report = self._report_text()

        self.assertIn("timestamp_granularity=lifecycle", report)
        self.assertIn("lifecycle-supported duration", report)
        self.assertIn("P4 并行度", report)
        self.assertIn("模型路由 ROI（观测）", report)
        self.assertIn("premium", report)
        self.assertIn("不推测成本或质量", report)

    def test_lifecycle_roi_reads_p2_artifact_pointer(self):
        self._prepare_p4_component(with_lifecycle=True)
        (self.run_dir / "artifacts" / "p2-routing.json").write_text(
            json.dumps(
                {"dag_tasks": [{"id": "orders", "recommended_model": "premium"}]}
            ),
            encoding="utf-8",
        )
        state_path = self.run_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["artifact_pointers"]["p2"] = "artifacts/p2-routing.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        report = self._report_text()

        self.assertIn("| premium | 1 | 1 |", report)

    def test_lifecycle_roi_marks_unassigned_component(self):
        self._prepare_p4_component(
            with_lifecycle=True,
            p2_design={"dag_tasks": [{"id": "orders"}]},
        )

        report = self._report_text()

        self.assertIn("未分配：组件 `orders`", report)

    def test_lifecycle_roi_marks_unmatched_component_unsupported(self):
        self._prepare_p4_component(
            with_lifecycle=True,
            p2_design={
                "dag_tasks": [
                    {"id": "billing", "recommended_model": "premium"},
                ]
            },
        )

        report = self._report_text()

        self.assertIn("unsupported：组件 `orders`：未匹配 P2 dag_tasks[].id", report)

    def test_lifecycle_roi_rejects_conflicting_p2_task_id_aliases(self):
        self._prepare_p4_component(
            with_lifecycle=True,
            p2_design={
                "dag_tasks": [
                    {
                        "id": "orders",
                        "task_id": "billing",
                        "recommended_model": "premium",
                    },
                ]
            },
        )

        report = self._report_text()

        self.assertIn("unsupported：P2 task ID schema冲突", report)
        self.assertNotIn("| premium |", report)

    def test_report_lists_warning_counts_and_accepted_reasons(self):
        self._event(
            "P4",
            "verification_recorded",
            {
                "tool": "ruff",
                "status": "PASS_WITH_ACCEPTED_WARNINGS",
                "artifact": "artifacts/ruff.txt",
                "counts": {"error": 0, "warning": 6, "info": 1},
                "accepted_warnings": [
                    {
                        "rule": "E501",
                        "path": "legacy.py",
                        "reason": "legacy generated prose",
                    }
                ] * 6,
            },
        )

        report = self._report_text()

        self.assertIn("0 errors / 6 warnings / 1 info", report)
        self.assertIn("legacy generated prose", report)

    def test_report_separates_accepted_preexisting_input_from_run_changes(self):
        manifest = json.loads((self.run_dir / "manifest.json").read_text(encoding="utf-8"))
        input_record = next(record for record in manifest["preexisting_files"] if record["path"] == "input.md")
        self._event(
            "P0",
            "baseline_accept",
            {
                "path": "input.md",
                "sha256": input_record["sha256"],
                "purpose": "provided input",
                "commit_allowed": True,
            },
        )
        (self.root / "new.py").write_text("value = 1\n", encoding="utf-8")

        report = self._report_text()

        self.assertIn("## 基线来源", report)
        self.assertIn("已接受的初始脏文件", report)
        self.assertIn("`input.md`", report)
        self.assertIn("本轮新增 1 个文件", report)
        new_files_section = report.split("本轮新增", 1)[1].split("## 基线来源", 1)[0]
        self.assertIn("`new.py`", new_files_section)
        self.assertNotIn("`input.md`", new_files_section)

    def test_report_lists_nested_untracked_files_individually(self):
        docs = self.root / "docs"
        nested_docs = docs / "nested"
        nested_docs.mkdir(parents=True)
        (docs / "a.md").write_text("first\n", encoding="utf-8")
        (nested_docs / "b file.md").write_text("second\n", encoding="utf-8")

        report = self._report_text()

        self.assertIn("本轮新增 2 个文件，共 2 行", report)
        new_files_section = report.split("本轮新增", 1)[1].split("## 基线来源", 1)[0]
        self.assertIn("`docs/a.md`", new_files_section)
        self.assertIn("`docs/nested/b file.md`", new_files_section)
        self.assertNotIn("`docs/`", new_files_section)

    def test_report_handles_nul_numstat_copy_path_with_spaces_and_unicode(self):
        source_path = self.root / "source.txt"
        source_path.write_text("original\n" * 20, encoding="utf-8")
        self._git("add", "source.txt")
        self._git("commit", "-qm", "add source")
        initialized = self._cc("init", "--slug", "nul-numstat", "--title", "NUL numstat")
        self.assertEqual(0, initialized.returncode, initialized.stderr)
        run_dir = Path(initialized.stdout.strip())

        copied_path = self.root / "复制 空格 文件.txt"
        copied_path.write_text(source_path.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
        self._git("add", copied_path.name)
        report = subprocess.run(
            ["python3", str(SCRIPT), "report", "--root", str(self.root), "--slug", "nul-numstat"],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, report.returncode, report.stderr)
        report_text = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("`复制 空格 文件.txt`", report_text)
        self.assertIn("| `复制 空格 文件.txt` | 1 | 0 |", report_text)
        self.assertNotIn("| `source.txt` |", report_text)
        self.assertNotIn("\\345", report_text)

    def test_report_rebuilds_verification_and_acceptance_from_events_after_state_tamper(self):
        verification = {
            "tool": "ruff",
            "status": "PASS",
            "artifact": "artifacts/ruff.txt",
            "counts": {"error": 0, "warning": 0, "info": 0},
        }
        self._event("P4", "verification_recorded", verification)
        manifest = json.loads((self.run_dir / "manifest.json").read_text(encoding="utf-8"))
        input_record = next(record for record in manifest["preexisting_files"] if record["path"] == "input.md")
        acceptance = {
            "path": "input.md",
            "sha256": input_record["sha256"],
            "purpose": "provided input",
            "commit_allowed": True,
        }
        self._event("P0", "baseline_accept", acceptance)
        state = self._state()
        state["verification_records"] = [{
            **verification,
            "tool": "forged-tool",
            "status": "PASS_WITH_ACCEPTED_WARNINGS",
        }]
        state["baseline_acceptances"] = [{
            **acceptance,
            "path": "forged.md",
            "purpose": "forged acceptance",
        }]
        tampered = self._cc("state", "--json", json.dumps(state))
        self.assertEqual(0, tampered.returncode, tampered.stderr)

        report = self._report_text()

        self.assertIn("**ruff**: PASS — 0 errors / 0 warnings / 0 info", report)
        self.assertIn("`input.md` — provided input", report)
        self.assertIn("unsupported：state.json 与 run.jsonl verification_recorded 不一致", report)
        self.assertIn("unsupported：state.json 与 run.jsonl baseline_accept 不一致", report)
        self.assertNotIn("forged-tool", report)
        self.assertNotIn("forged acceptance", report)

    def test_report_marks_missing_event_evidence_unsupported_instead_of_rendering_state_cache(self):
        state = self._state()
        state["verification_records"] = [{
            "tool": "forged-tool",
            "status": "PASS",
            "counts": {"error": 0, "warning": 0, "info": 0},
        }]
        state["baseline_acceptances"] = [{
            "path": "input.md",
            "purpose": "forged acceptance",
        }]
        tampered = self._cc("state", "--json", json.dumps(state))
        self.assertEqual(0, tampered.returncode, tampered.stderr)

        report = self._report_text()

        self.assertIn("unsupported：state.json 与 run.jsonl verification_recorded 不一致", report)
        self.assertIn("unsupported：state.json 与 run.jsonl baseline_accept 不一致", report)
        self.assertNotIn("forged-tool", report)
        self.assertNotIn("forged acceptance", report)


class EvidenceProtocolStaticContractTests(unittest.TestCase):
    def test_v117_contract_tokens_installer_loops_and_versions(self):
        repo_root = SCRIPT.parents[3]
        skill = repo_root / "skills" / "clean-architecture-autopilot" / "SKILL.md"
        skill_text = skill.read_text(encoding="utf-8")
        for token in (
            "design_amendment",
            "work_started",
            "work_finished",
            "verification_recorded",
            "debt_signoff_reused",
            "baseline_accept",
            "design_trace.py",
            "component_map.ownership",
        ):
            with self.subTest(token=token):
                self.assertIn(token, skill_text)

        installer = (repo_root / "install.sh").read_text(encoding="utf-8")
        self.assertIn(
            "for py in cc_log.py dep_graph.py design_coverage.py plan_graph.py design_trace.py; do",
            installer,
        )
        self.assertEqual(2, installer.count("design_trace.py"))

        for skill_path in (repo_root / "skills").glob("*/SKILL.md"):
            with self.subTest(skill=skill_path):
                self.assertIn(
                    "<!-- clean-architecture system v1.17.0 -->",
                    skill_path.read_text(encoding="utf-8"),
                )
        for agent_path in (repo_root / "agents").glob("*.md"):
            with self.subTest(agent=agent_path):
                self.assertIn("version: 1.17.0", agent_path.read_text(encoding="utf-8"))

    def test_component_ownership_example_passes_dep_graph_validation(self):
        repo_root = SCRIPT.parents[3]
        skill = repo_root / "skills" / "clean-architecture-autopilot" / "SKILL.md"
        skill_text = skill.read_text(encoding="utf-8")
        ownership_section = skill_text.index("**P2 component ownership.**")
        example_start = skill_text.index(
            '{"component_map":{"ownership":', ownership_section
        )
        example_end = skill_text.index("\n```", example_start)
        component_map = json.loads(skill_text[example_start:example_end])

        with tempfile.TemporaryDirectory() as temp_dir:
            component_map_path = Path(temp_dir) / "component-map.json"
            component_map_path.write_text(json.dumps(component_map), encoding="utf-8")
            _, errors = dep_graph.load_component_ownership(component_map_path)

        self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
