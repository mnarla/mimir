"""Synthetic requests only; these fixtures contain no user conversation data."""

import copy
import importlib
import importlib.util
import json
import re
import unittest
from pathlib import Path

TIME = "2026-11-23T10:00:00Z"


def record():
    return {
        "project_id": "p_test",
        "idempotency_key": "request1",
        "identity": {"entity": "engine", "key": "storage", "scope": "repo"},
        "statement": "Use SQLite",
        "payload": {
            "type": "Decision",
            "data": {"choice": {"kind": "string", "value": "SQLite"}, "rationale": "Local storage"},
        },
        "cardinality": "single",
        "valid_from": TIME,
        "valid_to": None,
        "expected_revision": None,
        "supersedes_revision": None,
        "source": {
            "source_tool": "test",
            "source_agent": None,
            "session_id": "session1",
            "timestamp": TIME,
            "anchor": "synthetic line 1",
            "locator": None,
        },
    }


def batch():
    r = record()
    return {
        "adapter_id": "test",
        "project_id": "p_test",
        "batch_id": "batch1",
        "expected_checkpoint": None,
        "next_checkpoint": "checkpoint1",
        "entries": [
            {
                "source_id": "source1",
                "source_revision": "r1",
                "sanitized_digest": "a" * 64,
                "source": r["source"],
                "records": [r],
                "extraction_state": "structured",
            }
        ],
    }


class ContractTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(
            importlib.util.find_spec("brain.contracts"), "missing contract boundary"
        )
        return importlib.import_module("brain.contracts")

    def test_registry_matches_research_and_exports_local_object_schemas(self):
        api = self.api()
        research = Path("research/04-mcp-and-retrieval.md").read_text()
        registry = json.loads(re.search(r"```json\n(.*?)\n```", research, re.S)[1])
        for name in api.CONTRACTS:
            for direction in ["Input", "Output"]:
                schema = api.schema_for(name, direction)
                self.assertEqual(schema["$defs"], registry["$defs"])
                self.assertEqual(schema["type"], "object")
                schema["$defs"].clear()  # Returned schemas must not mutate cached definitions.
        self.assertTrue(api.schema_for("record_memory", "Input")["$defs"])

    def test_prepares_copy_normalizes_time_and_scrubs_nested_content(self):
        api = self.api()
        r = record()
        secret = "sk-" + "x" * 40
        r["statement"] += " api_key=" + secret
        r["payload"]["data"]["rationale"] = "Bearer " + secret
        original = copy.deepcopy(r)
        clean = api.prepare_input("record_memory", r)
        self.assertEqual(r, original)
        self.assertNotIn(secret, json.dumps(clean))
        self.assertIn("[REDACTED]", clean["statement"])
        self.assertEqual(clean["source"]["timestamp"], "2026-11-23T10:00:00.000000Z")

    def test_all_five_typed_cards_pass_with_matching_identities(self):
        api = self.api()
        variants = [
            (
                "Constraint",
                {"operator": "le", "value": {"kind": "number", "value": 300, "unit": "MB"}},
            ),
            ("TechStack", {"technology": "SQLite", "version": None}),
            ("TaskState", {"task_id": "engine", "state": "in_progress", "transition": "set"}),
            (
                "BugFix",
                {
                    "issue_id": "engine",
                    "symptom": "synthetic failure",
                    "cause": "fixture",
                    "fix": "synthetic fix",
                    "verification": None,
                },
            ),
        ]
        api.prepare_input("record_memory", record())
        for kind, data in variants:
            r = record()
            r["payload"] = {"type": kind, "data": data}
            with self.subTest(kind=kind):
                api.prepare_input("record_memory", r)

    def test_rejects_nested_unknown_fields_wrong_types_and_raw_transcripts(self):
        api = self.api()
        cases = []
        r = record()
        r["source"]["cookies"] = "no"
        cases.append(r)
        r = record()
        r["project_id"] = 12
        cases.append(r)
        r = record()
        r["payload"]["data"]["choice"] = {"kind": "number", "value": True, "unit": None}
        cases.append(r)
        r = record()
        r["transcript"] = "raw text"
        cases.append(r)
        for r in cases:
            with self.subTest(r=r), self.assertRaises(api.ContractError):
                api.prepare_input("record_memory", r)
        b = batch()
        b["entries"][0]["messages"] = []
        with self.assertRaises(api.ContractError):
            api.prepare_input("ingest", b)

    def test_strict_json_parser_rejects_duplicates_nonfinite_and_oversize(self):
        api = self.api()
        for raw in [
            b'{"project_id":"a","project_id":"b"}',
            b'{"x":NaN}',
            b'{"x":Infinity}',
            b'{"x":1e999}',
            b"[]",
            b"{invalid",
            b"\xff",
            b" " * (256 * 1024 + 1),
        ]:
            with self.subTest(raw=raw[:50]), self.assertRaises(api.ContractError):
                api.prepare_input("record_memory", raw)
        r = record()
        r["payload"]["data"]["choice"] = {"kind": "number", "value": float("nan"), "unit": None}
        with self.assertRaises(api.ContractError):
            api.prepare_input("record_memory", r)
        self.assertEqual(
            api.prepare_input("record_memory", json.dumps(record()).encode())["project_id"],
            "p_test",
        )

    def test_time_interval_and_task_identity_rules(self):
        api = self.api()
        for timestamp in [
            "yesterday",
            "2026-11-23T11:00:00+01:00",
            "2026-02-30T10:00:00Z",
            "2026-11-23T10:00:00.1234567Z",
            "2026-11-23T10:00:60Z",
        ]:
            r = record()
            r["source"]["timestamp"] = timestamp
            with self.subTest(timestamp=timestamp), self.assertRaises(api.ContractError):
                api.prepare_input("record_memory", r)
        for start, end in [(TIME, TIME), (None, TIME), (TIME, "2026-01-01T00:00:00Z")]:
            r = record()
            r["valid_from"], r["valid_to"] = start, end
            with self.assertRaises(api.ContractError):
                api.prepare_input("record_memory", r)
        for kind, data in [
            ("TaskState", {"task_id": "other", "state": "done", "transition": "set"}),
            (
                "BugFix",
                {
                    "issue_id": "other",
                    "symptom": "x",
                    "cause": "x",
                    "fix": "x",
                    "verification": None,
                },
            ),
        ]:
            r = record()
            r["payload"] = {"type": kind, "data": data}
            with self.assertRaises(api.ContractError):
                api.prepare_input("record_memory", r)

    def test_ingest_cross_field_rules_and_no_identity_redaction(self):
        api = self.api()
        for mutation in ["project", "source", "state", "empty", "duplicate"]:
            b = batch()
            entry = b["entries"][0]
            if mutation == "project":
                entry["records"][0]["project_id"] = "other"
            if mutation == "source":
                entry["records"][0]["source"] = dict(entry["source"], anchor="other")
            if mutation == "state":
                entry["extraction_state"] = "needs_agent"
            if mutation == "empty":
                entry["records"] = []
            if mutation == "duplicate":
                b["entries"].append(copy.deepcopy(entry))
            with self.subTest(mutation=mutation), self.assertRaises(api.ContractError):
                api.prepare_input("ingest", b)
        self.assertEqual(
            api.prepare_input("ingest", batch())["entries"][0]["extraction_state"], "structured"
        )
        r = record()
        r["identity"]["key"] = "api_key=" + "sk-" + "x" * 30
        with self.assertRaises(api.ContractError):
            api.prepare_input("record_memory", r)

    def test_redaction_cannot_hide_a_source_identity_mismatch(self):
        api = self.api()
        b = batch()
        b["entries"][0]["source"]["anchor"] = "api_key=" + "sk-" + "a" * 30
        b["entries"][0]["records"][0]["source"] = dict(
            b["entries"][0]["source"], anchor="api_key=" + "sk-" + "b" * 30
        )
        with self.assertRaises(api.ContractError):
            api.prepare_input("ingest", b)

    def test_read_and_resolution_cross_field_rules(self):
        api = self.api()
        scope = {
            "project_id": "p_test",
            "additional_projects": [],
            "valid_at": None,
            "known_seq": None,
            "token_budget": 1024,
            "tokenizer": "utf8_bytes",
        }
        r = {"scope": scope, "goal": None}
        api.prepare_input("get_project_context", r)
        scope["tokenizer"] = "configured"
        with self.assertRaises(api.ContractError):
            api.prepare_input("get_project_context", r)
        api.prepare_input("get_project_context", r, configured_tokenizer=True)
        scope["token_budget"] = 1024.0
        with self.assertRaises(api.ContractError):
            api.prepare_input("get_project_context", r)
        h = {
            "project_id": "p_test",
            "memory_id": "m1",
            "after_seq": 3,
            "through_seq": 2,
            "limit": 10,
            "token_budget": 1024,
            "tokenizer": "utf8_bytes",
        }
        with self.assertRaises(api.ContractError):
            api.prepare_input("get_history", h)
        review = {
            "project_id": "p_test",
            "conflict_id": "c1",
            "expected_conflict_seq": 1,
            "action": "accept_candidate",
            "effective_at": None,
            "idempotency_key": "r1",
            "rationale": "synthetic review",
        }
        with self.assertRaises(api.ContractError):
            api.prepare_input("resolve_conflict", review)

    def test_all_output_schemas_accept_synthetic_success_and_error_envelopes(self):
        api = self.api()
        from jsonschema import Draft202012Validator

        definitions = api.schema_for("record_memory", "Input")["$defs"]

        def sample(schema):
            if "$ref" in schema:
                return sample(definitions[schema["$ref"].split("/")[-1]])
            if "const" in schema:
                return schema["const"]
            if "enum" in schema:
                return schema["enum"][0]
            if "oneOf" in schema:
                return sample(schema["oneOf"][0])
            if "anyOf" in schema:
                return None
            kind = schema["type"]
            if kind == "object":
                return {k: sample(v) for k, v in schema["properties"].items()}
            if kind == "array":
                return []
            if kind == "string":
                return TIME if schema.get("format") == "date-time" else "synthetic"
            if kind in ["integer", "number"]:
                return schema.get("minimum", 0)
            if kind == "boolean":
                return False
            raise AssertionError(schema)

        for name in api.CONTRACTS:
            with self.subTest(name=name):
                output = api.schema_for(name, "Output")
                Draft202012Validator.check_schema(output)
                api.validate_output(name, sample(output))
                api.validate_output(
                    name,
                    {
                        "ok": False,
                        "error": {
                            "code": "invalid_input",
                            "message": "Invalid request",
                            "retryable": False,
                        },
                    },
                )
        response = sample(api.schema_for("search_memory", "Output"))
        response["data"]["budget"]["used"] = 999999
        with self.assertRaises(api.ContractError):
            api.validate_output("search_memory", response)

    def test_errors_never_echo_values_and_outputs_are_not_silently_scrubbed(self):
        api = self.api()
        secret = "ghp_" + "z" * 40
        bad = record()
        bad["unexpected"] = secret
        with self.assertRaises(api.ContractError) as caught:
            api.prepare_input("record_memory", bad)
        self.assertNotIn(secret, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        error = {
            "ok": False,
            "error": {"code": "invalid_input", "message": "Invalid request", "retryable": False},
        }
        api.validate_output("record_memory", error)
        error["error"]["message"] = secret
        with self.assertRaises(api.ContractError):
            api.validate_output("record_memory", error)
        for name in ["unknown", "../../etc/passwd"]:
            with self.assertRaises(api.ContractError):
                api.schema_for(name, "Input")
