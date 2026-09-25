#!/usr/bin/env python3
"""Regression tests for component ownership support in dep_graph.py."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import dep_graph


class ComponentGraphTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        package = self.root / "pkg"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "home.py").write_text(
            "from pkg.context import Context\n", encoding="utf-8"
        )
        (package / "context.py").write_text(
            "from pkg.valuation import Valuation\n", encoding="utf-8"
        )
        (package / "valuation.py").write_text("", encoding="utf-8")
        self.component_map = self.root / "p2.json"
        self.component_map.write_text(
            json.dumps(
                {
                    "component_map": {
                        "ownership": [
                            {"module": "pkg", "component": "package", "kind": "framework"},
                            {"module": "pkg.home", "component": "home", "kind": "domain"},
                            {
                                "module": "pkg.context",
                                "component": "context",
                                "kind": "shared_adapter",
                            },
                            {
                                "module": "pkg.valuation",
                                "component": "valuation",
                                "kind": "adapter",
                            },
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _module_graph(self):
        files = dep_graph.collect_files(str(self.root), ["pkg"], [])
        graph, _, _ = dep_graph.build_graph(str(self.root), files)
        return graph

    def test_shared_adapter_is_its_own_component_edge(self):
        ownership = dep_graph.load_component_ownership(self.component_map)
        result = dep_graph.component_graph(self._module_graph(), ownership)

        self.assertIn({"from": "home", "to": "context"}, result["component_edges"])
        self.assertIn({"from": "context", "to": "valuation"}, result["component_edges"])
        self.assertEqual([], result["ownership_errors"])

    def test_unowned_module_is_reported(self):
        result = dep_graph.component_graph({"pkg.unowned": set()}, ownership=[])

        self.assertEqual(["pkg.unowned"], result["unowned_modules"])

    def test_duplicate_module_and_illegal_kind_are_ownership_errors(self):
        self.component_map.write_text(
            json.dumps(
                {
                    "component_map": {
                        "ownership": [
                            {"module": "pkg.home", "component": "home", "kind": "domain"},
                            {"module": "pkg.home", "component": "other", "kind": "adapter"},
                            {
                                "module": "pkg.context",
                                "component": "context",
                                "kind": "unknown",
                            },
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        ownership = dep_graph.load_component_ownership(self.component_map)
        result = dep_graph.component_graph(self._module_graph(), ownership)

        self.assertEqual("home", ownership[0]["pkg.home"]["component"])
        self.assertEqual(2, len(result["ownership_errors"]))
        self.assertIn("duplicate module ownership", result["ownership_errors"][0]["message"])
        self.assertIn("invalid kind", result["ownership_errors"][1]["message"])

    def test_component_cycle_uses_existing_tarjan_implementation(self):
        ownership = (
            {
                "pkg.home": {"component": "home", "kind": "domain"},
                "pkg.context": {"component": "context", "kind": "adapter"},
            },
            [],
        )
        result = dep_graph.component_graph(
            {"pkg.home": {"pkg.context"}, "pkg.context": {"pkg.home"}}, ownership
        )

        self.assertEqual([["context", "home"]], result["component_sccs"])

    def test_ownership_records_require_exact_nonempty_schema(self):
        self.component_map.write_text(
            json.dumps(
                {
                    "component_map": {
                        "ownership": [
                            {"module": "", "component": "home", "kind": "domain"},
                            {
                                "module": "pkg.context",
                                "component": "context",
                                "kind": "adapter",
                                "extra": "not allowed",
                            },
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        ownership, errors = dep_graph.load_component_ownership(self.component_map)

        self.assertEqual({}, ownership)
        self.assertEqual(2, len(errors))
        self.assertIn("non-empty string", errors[0]["message"])
        self.assertIn("exactly", errors[1]["message"])

    def test_ownership_identifiers_must_be_canonical(self):
        self.component_map.write_text(
            json.dumps(
                {
                    "component_map": {
                        "ownership": [
                            {"module": " pkg.home", "component": "home", "kind": "domain"},
                            {"module": "pkg.home ", "component": "home", "kind": "domain"},
                            {"module": "pkg/home", "component": "home", "kind": "domain"},
                            {"module": "pkg.home.py", "component": "home", "kind": "domain"},
                            {"module": "pkg..home", "component": "home", "kind": "domain"},
                            {"module": "pkg.1home", "component": "home", "kind": "domain"},
                            {"module": "pkg.home", "component": " home", "kind": "domain"},
                            {"module": "pkg.context", "component": "context ", "kind": "adapter"},
                            {"module": "pkg.valuation", "component": "context/valuation", "kind": "adapter"},
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        ownership, errors = dep_graph.load_component_ownership(self.component_map)

        self.assertEqual({}, ownership)
        self.assertEqual(9, len(errors))
        self.assertTrue(all("identifier" in error["message"] for error in errors))

    def test_ownership_errors_use_message_only_schema(self):
        self.component_map.write_text(
            json.dumps(
                {
                    "component_map": {
                        "ownership": [
                            {"module": "pkg.home", "component": "home", "kind": "invalid"},
                            {"module": "pkg.home", "component": "other", "kind": "domain"},
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        _, load_errors = dep_graph.load_component_ownership(self.component_map)
        graph_errors = dep_graph.component_graph(
            {"pkg.home": set()},
            {"pkg.home": {"kind": "domain"}, "pkg.missing": {"component": "other"}},
        )["ownership_errors"]

        for error in [*load_errors, *graph_errors]:
            with self.subTest(error=error):
                self.assertEqual({"message"}, set(error))
                self.assertIsInstance(error["message"], str)
                self.assertTrue(error["message"].strip())

    def test_canonical_dotted_identifier_rejects_keywords_in_every_part(self):
        for identifier in ("class", "class.pkg", "pkg.class", "pkg.from.value"):
            with self.subTest(identifier=identifier):
                self.assertFalse(dep_graph.is_canonical_dotted_identifier(identifier))

    def test_cli_component_map_ownership_errors_exit_one(self):
        artifact = json.loads(self.component_map.read_text(encoding="utf-8"))
        artifact["component_map"]["ownership"].append(
            {"module": "pkg.unknown", "component": "unknown", "kind": "framework"}
        )
        self.component_map.write_text(json.dumps(artifact), encoding="utf-8")
        output = self.root / "graph.json"

        completed = subprocess.run(
            [
                "python3", str(Path(dep_graph.__file__)), "--root", str(self.root),
                "--scan-dirs", "pkg", "--component-map", str(self.component_map),
                "--json", str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual([], result["component_graph"]["unowned_modules"])
        self.assertIn(
            "not in the scanned module graph",
            result["component_graph"]["ownership_errors"][0]["message"],
        )

    def test_cli_component_map_unowned_modules_exit_one(self):
        artifact = json.loads(self.component_map.read_text(encoding="utf-8"))
        artifact["component_map"]["ownership"] = artifact["component_map"]["ownership"][:-1]
        self.component_map.write_text(json.dumps(artifact), encoding="utf-8")
        output = self.root / "graph.json"

        completed = subprocess.run(
            [
                "python3", str(Path(dep_graph.__file__)), "--root", str(self.root),
                "--scan-dirs", "pkg", "--component-map", str(self.component_map),
                "--json", str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(["pkg.valuation"], result["component_graph"]["unowned_modules"])

    def test_cli_component_projection_cycle_exits_one_without_module_scc(self):
        (self.root / "pkg" / "a_one.py").write_text(
            "from pkg.b_one import BOne\n", encoding="utf-8"
        )
        (self.root / "pkg" / "a_two.py").write_text("", encoding="utf-8")
        (self.root / "pkg" / "b_one.py").write_text("", encoding="utf-8")
        (self.root / "pkg" / "b_two.py").write_text(
            "from pkg.a_two import ATwo\n", encoding="utf-8"
        )
        projection_map = self.root / "projection-map.json"
        projection_map.write_text(
            json.dumps(
                {
                    "component_map": {
                        "ownership": [
                            {"module": "pkg", "component": "package", "kind": "framework"},
                            {"module": "pkg.a_one", "component": "component_a", "kind": "domain"},
                            {"module": "pkg.a_two", "component": "component_a", "kind": "domain"},
                            {"module": "pkg.b_one", "component": "component_b", "kind": "adapter"},
                            {"module": "pkg.b_two", "component": "component_b", "kind": "adapter"},
                            {"module": "pkg.context", "component": "context", "kind": "adapter"},
                            {"module": "pkg.home", "component": "home", "kind": "domain"},
                            {"module": "pkg.valuation", "component": "valuation", "kind": "adapter"},
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        output = self.root / "graph.json"

        completed = subprocess.run(
            [
                "python3", str(Path(dep_graph.__file__)), "--root", str(self.root),
                "--scan-dirs", "pkg", "--component-map", str(projection_map),
                "--json", str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual([], result["sccs_gt1"])
        self.assertEqual(
            [["component_a", "component_b"]],
            result["component_graph"]["component_sccs"],
        )

    def test_cli_without_component_map_preserves_module_only_success(self):
        output = self.root / "graph.json"

        completed = subprocess.run(
            [
                "python3", str(Path(dep_graph.__file__)), "--root", str(self.root),
                "--scan-dirs", "pkg", "--json", str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertNotIn("component_graph", result)

    def test_cli_unreadable_component_map_exits_two(self):
        missing_map = self.root / "missing.json"

        completed = subprocess.run(
            [
                "python3", str(Path(dep_graph.__file__)), "--root", str(self.root),
                "--scan-dirs", "pkg", "--component-map", str(missing_map),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(2, completed.returncode)
        self.assertIn("cannot read --component-map", completed.stderr)

    def test_cli_empty_component_map_exits_two(self):
        completed = subprocess.run(
            [
                "python3", str(Path(dep_graph.__file__)), "--root", str(self.root),
                "--scan-dirs", "pkg", "--component-map", "",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(2, completed.returncode)
        self.assertIn("cannot read --component-map", completed.stderr)

    def test_cli_invalid_component_map_json_exits_two(self):
        self.component_map.write_text("{invalid json", encoding="utf-8")

        completed = subprocess.run(
            [
                "python3", str(Path(dep_graph.__file__)), "--root", str(self.root),
                "--scan-dirs", "pkg", "--component-map", str(self.component_map),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(2, completed.returncode)
        self.assertIn("cannot read --component-map", completed.stderr)

    def test_cli_non_utf8_component_map_exits_two_without_traceback(self):
        self.component_map.write_bytes(b"\xff")

        completed = subprocess.run(
            [
                "python3", str(Path(dep_graph.__file__)), "--root", str(self.root),
                "--scan-dirs", "pkg", "--component-map", str(self.component_map),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(2, completed.returncode)
        self.assertIn("cannot read --component-map", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_cli_component_map_adds_component_graph_to_json_output(self):
        output = self.root / "graph.json"
        completed = subprocess.run(
            [
                "python3",
                str(Path(dep_graph.__file__)),
                "--root",
                str(self.root),
                "--scan-dirs",
                "pkg",
                "--component-map",
                str(self.component_map),
                "--json",
                str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(2, len(result["component_graph"]["component_edges"]))
        self.assertIn("component_edges=2", completed.stdout)


if __name__ == "__main__":
    unittest.main()
