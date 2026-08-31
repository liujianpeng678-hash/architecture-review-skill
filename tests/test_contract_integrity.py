"""Adversarial tests for the audit semantic-integrity trust chain."""

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest

from test_scripts import Base, complete_architecture, run, write

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
STANDARD = os.path.normpath(os.path.join(HERE, "..", "reference", "standard.json"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from audit_contract import AuditContract, AuditContractError
from audit_state import AuditStateRepository


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


class TestSemanticIntegrity(Base):
    def _prepare(self, project_type="game"):
        codemap = os.path.join(self.d, ".codemap")
        os.makedirs(codemap, exist_ok=True)
        self.state = os.path.join(codemap, "modules.json")
        if project_type == "game":
            write(os.path.join(self.d, "project.godot"),
                  "[application]\nconfig/name=\"Semantic Demo\"\n")
        else:
            write(os.path.join(self.d, "package.json"),
                  json.dumps({"dependencies": {"react": "1"}}))
        write(os.path.join(self.d, "src/a.py"), "x = 1\n")
        dimensions, lenses = complete_architecture()
        self.save({
            "meta": {"project": "Semantic Demo",
                     "htmlPath": ".codemap/codemap.html",
                     "mdPath": ".codemap/codemap.md"},
            "bands": [{"id": "b", "t": "B"}],
            "spine": [],
            "architectureDimensions": dimensions,
            "architectureLenses": lenses,
            "modules": [{
                "id": "m_a", "label": "A", "band": "b", "coupling": "low",
                "deps": [], "paths": ["src/a.py"],
            }],
        })
        scanned = run("scan.py", "--root", self.d, "--state", self.state, "--write")
        self.assertEqual(scanned.returncode, 0, scanned.stderr)
        state = self.load()
        state["modules"][0].update({
            "auditedHash": state["modules"][0]["contentHash"],
            "score": 92,
            "grade": "A",
            "tags": ["clean"],
            "findings": [],
        })
        self.save(state)
        write(os.path.join(codemap, "codemap.html"), "<html>map</html>")
        write(os.path.join(codemap, "codemap.md"), "# map\n")
        write(os.path.join(codemap, "config.json"), "{}\n")

    def _publish(self, *extra):
        return run("version.py", "publish", "--root", self.d, *extra)

    def test_apply_rejection_keeps_working_state_bytes(self):
        self._prepare()
        with open(self.state, "rb") as handle:
            before = handle.read()
        result = run(
            "apply_audit.py", "--state", self.state, "--id", "m_a", "--json",
            json.dumps({"score": 70, "grade": "A", "tags": ["clean"], "findings": []}),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AUDIT_GRADE_MISMATCH", result.stderr)
        with open(self.state, "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertFalse(os.path.exists(self.state + ".audit-state.lock"))

    def test_publish_rejects_direct_semantic_tamper_matrix(self):
        self._prepare()
        valid = self.load()
        cases = {
            "bool-score": (lambda module: module.update(score=True), "AUDIT_SCORE_TYPE"),
            "score-range": (lambda module: module.update(score=101), "AUDIT_SCORE_RANGE"),
            "grade-mismatch": (lambda module: module.update(score=70, grade="A"),
                               "AUDIT_GRADE_MISMATCH"),
            "clean-conflict": (lambda module: module.update(
                tags=["clean", "legacy"],
                findings=[{"sev": "LOW", "loc": "src/a.py:1", "text": "legacy"}]),
                "AUDIT_CLEAN_CONFLICT"),
            "unknown-tag": (lambda module: module.update(
                tags=["invented"],
                findings=[{"sev": "LOW", "loc": "src/a.py:1", "text": "x"}]),
                "AUDIT_TAG_UNKNOWN"),
            "missing-evidence": (lambda module: module.update(tags=["legacy"], findings=[]),
                                 "AUDIT_PROBLEM_WITHOUT_FINDING"),
            "hash-mismatch": (lambda module: module.update(auditedHash="forged"),
                              "AUDIT_HASH_MISMATCH"),
        }
        for name, (tamper, expected_code) in cases.items():
            with self.subTest(name=name):
                candidate = copy.deepcopy(valid)
                tamper(candidate["modules"][0])
                self.save(candidate)
                with open(self.state, "rb") as handle:
                    before = handle.read()
                result = self._publish("--mode", "full")
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(expected_code, result.stderr)
                with open(self.state, "rb") as handle:
                    self.assertEqual(handle.read(), before)
                versions = os.path.join(self.d, ".codemap", "versions")
                self.assertFalse(os.path.isfile(os.path.join(versions, "index.json")))
                self.assertFalse(os.path.isdir(os.path.join(versions, "audit-v0001")))
                self.save(valid)

    def test_future_working_schema_is_rejected_without_version_allocation(self):
        self._prepare()
        state = self.load()
        state["schemaVersion"] = 99
        self.save(state)
        result = self._publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SCHEMA_FUTURE_UNSUPPORTED", result.stderr)
        self.assertFalse(os.path.exists(
            os.path.join(self.d, ".codemap", "versions", "index.json")))

    def test_failed_structured_gate_cannot_publish(self):
        self._prepare()
        result = self._publish("--gate", "unit-tests:fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PREFLIGHT_GATE_FAILED", result.stderr)
        self.assertFalse(os.path.exists(
            os.path.join(self.d, ".codemap", "versions", "index.json")))

    def test_allow_incomplete_never_bypasses_semantic_contract(self):
        self._prepare()
        state = self.load()
        state["modules"][0]["score"] = True
        self.save(state)
        result = self._publish("--allow-incomplete")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AUDIT_SCORE_TYPE", result.stderr)
        self.assertFalse(os.path.exists(
            os.path.join(self.d, ".codemap", "versions", "index.json")))

    def test_new_version_has_receipt_and_layered_verification(self):
        self._prepare("website")
        published = self._publish("--mode", "full", "--expected-baseline", "none",
                                  "--gate", "unit-tests:pass")
        self.assertEqual(published.returncode, 0, published.stderr)
        base = os.path.join(self.d, ".codemap", "versions", "audit-v0001")
        receipt_path = os.path.join(base, "semantic-receipt.json")
        self.assertTrue(os.path.isfile(receipt_path))
        with open(receipt_path, encoding="utf-8") as handle:
            receipt = json.load(handle)
        self.assertTrue(receipt["semanticValid"])
        self.assertEqual(receipt["projectType"], "website")
        self.assertEqual(receipt["validationGates"], ["unit-tests:pass"])
        verified = run("version.py", "verify", "--root", self.d)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        result = json.loads(verified.stdout)
        self.assertTrue(result["integrityValid"])
        self.assertTrue(result["semanticValid"])
        self.assertEqual(result["compatibilityStatus"], "current-contract")
        self.assertEqual(result["versions"][0]["stateFingerprint"],
                         receipt["stateFingerprint"])

    def test_verify_rejects_semantic_tamper_even_after_hashes_are_recomputed(self):
        self._prepare()
        self.assertEqual(self._publish("--gate", "unit-tests:pass").returncode, 0)
        versions = os.path.join(self.d, ".codemap", "versions")
        base = os.path.join(versions, "audit-v0001")
        retained_state_path = os.path.join(base, "modules.json")
        with open(retained_state_path, encoding="utf-8") as handle:
            retained = json.load(handle)
        retained["modules"][0]["score"] = 70
        retained["modules"][0]["grade"] = "A"
        write_json(retained_state_path, retained)
        write_json(self.state, retained)

        manifest_path = os.path.join(base, "manifest.json")
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        manifest["artifacts"]["modules.json"] = sha256_file(retained_state_path)
        write_json(manifest_path, manifest)
        index_path = os.path.join(versions, "index.json")
        with open(index_path, encoding="utf-8") as handle:
            index = json.load(handle)
        index["versions"][0]["manifestSha256"] = sha256_file(manifest_path)
        write_json(index_path, index)

        verified = run("version.py", "verify", "--root", self.d)
        self.assertNotEqual(verified.returncode, 0)
        result = json.loads(verified.stdout)
        self.assertTrue(result["integrityValid"], result["errors"])
        self.assertFalse(result["semanticValid"])
        self.assertIn("AUDIT_GRADE_MISMATCH", verified.stdout)

    def test_legacy_v1_history_is_read_only_compatible(self):
        self._prepare()
        self.assertEqual(self._publish().returncode, 0)
        versions = os.path.join(self.d, ".codemap", "versions")
        base = os.path.join(versions, "audit-v0001")
        retained_state_path = os.path.join(base, "modules.json")
        with open(retained_state_path, encoding="utf-8") as handle:
            retained = json.load(handle)
        retained.pop("schemaVersion", None)
        retained.pop("semanticValidation", None)
        write_json(retained_state_path, retained)
        write_json(self.state, retained)

        receipt_path = os.path.join(base, "semantic-receipt.json")
        os.unlink(receipt_path)
        manifest_path = os.path.join(base, "manifest.json")
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        manifest.pop("semanticValidation", None)
        manifest["toolVersion"] = "2.0"
        manifest["artifacts"].pop("semantic-receipt.json", None)
        manifest["artifacts"]["modules.json"] = sha256_file(retained_state_path)
        write_json(manifest_path, manifest)

        index_path = os.path.join(versions, "index.json")
        with open(index_path, encoding="utf-8") as handle:
            index = json.load(handle)
        index["versions"][0].pop("semanticValid", None)
        index["versions"][0].pop("contractFingerprint", None)
        index["versions"][0]["manifestSha256"] = sha256_file(manifest_path)
        write_json(index_path, index)

        verified = run("version.py", "verify", "--root", self.d)
        self.assertEqual(verified.returncode, 0, verified.stderr + verified.stdout)
        result = json.loads(verified.stdout)
        self.assertTrue(result["integrityValid"])
        self.assertTrue(result["semanticValid"])
        self.assertEqual(result["compatibilityStatus"], "legacy-contract")
        self.assertEqual(result["versions"][0]["compatibilityStatus"], "legacy-contract")

    def test_repository_rejects_stale_fingerprint_without_overwrite(self):
        self._prepare()
        repository = AuditStateRepository(self.state, AuditContract.from_path(STANDARD))
        loaded = repository.load_working_state()
        concurrent = copy.deepcopy(loaded["state"])
        concurrent.setdefault("meta", {})["concurrentMarker"] = "preserve-me"
        self.save(concurrent)
        with self.assertRaises(AuditContractError) as raised:
            repository.commit(loaded["state"], loaded["expectedFingerprint"])
        self.assertIn("STATE_BASELINE_MISMATCH", str(raised.exception))
        self.assertEqual(self.load()["meta"]["concurrentMarker"], "preserve-me")
        self.assertFalse(os.path.exists(self.state + ".audit-state.lock"))

    def test_game_full_incremental_verify_and_rollback_real_cli_path(self):
        self._prepare("game")
        first = self._publish(
            "--mode", "full", "--expected-baseline", "none",
            "--gate", "focused-tests:pass",
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["version"], "audit-v0001")

        write(os.path.join(self.d, "src/a.py"), "x = 2\n")
        scanned = run("scan.py", "--root", self.d, "--state", self.state, "--write")
        self.assertEqual(scanned.returncode, 0, scanned.stderr)
        self.assertIn("m_a", json.loads(scanned.stdout)["stale"])
        applied = run(
            "apply_audit.py", "--state", self.state, "--id", "m_a", "--json",
            json.dumps({"score": 92, "grade": "A", "tags": ["clean"], "findings": []}),
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        state = self.load()
        self.assertEqual(state["modules"][0]["auditedHash"],
                         state["modules"][0]["contentHash"])

        second = self._publish(
            "--mode", "incremental", "--expected-baseline", "audit-v0001",
            "--scope", "m_a", "--gate", "focused-tests:pass",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["version"], "audit-v0002")
        versions = os.path.join(self.d, ".codemap", "versions")
        for version in ("audit-v0001", "audit-v0002"):
            self.assertTrue(os.path.isfile(
                os.path.join(versions, version, "semantic-receipt.json")))

        verified = run("version.py", "verify", "--root", self.d)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        verification = json.loads(verified.stdout)
        self.assertTrue(verification["integrityValid"])
        self.assertTrue(verification["semanticValid"])

        rolled = run(
            "version.py", "rollback", "--root", self.d,
            "--to", "audit-v0001", "--reason", "semantic-chain recovery proof",
        )
        self.assertEqual(rolled.returncode, 0, rolled.stderr)
        verified_after = run("version.py", "verify", "--root", self.d)
        self.assertEqual(verified_after.returncode, 0, verified_after.stderr)
        after = json.loads(verified_after.stdout)
        self.assertEqual(after["latestVersion"], "audit-v0002")
        self.assertEqual(after["activeVersion"], "audit-v0001")
        self.assertTrue(after["integrityValid"])
        self.assertTrue(after["semanticValid"])


if __name__ == "__main__":
    unittest.main()
