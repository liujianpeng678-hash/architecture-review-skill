"""Golden / behavior tests for the codemap scripts. Stdlib only — run with:

    python -m unittest discover -s tests -v

Each test drives the real CLI (subprocess) against a throwaway fixture, so it tests
exactly what an agent or CI runs.
"""
import json, os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
TEMPLATE = os.path.normpath(os.path.join(HERE, "..", "assets", "template.html"))


def run(script, *args):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def complete_architecture(module_id="m_a"):
    dimensions = []
    for dim_id in ("responsibility", "boundary", "contract", "dependency",
                   "data_logic", "composition_state", "evolution", "safeguards"):
        dimensions.append({
            "id": dim_id,
            "status": "good",
            "score": None,
            "summary": "evidence supports " + dim_id,
            "evidence": [],
            "relatedModules": [module_id],
        })
    lenses = [{"id": lens_id, "summary": "summary " + lens_id}
              for lens_id in ("split", "connect", "change", "protect")]
    return dimensions, lenses


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.state = os.path.join(self.d, "modules.json")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def save(self, state):
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)

    def load(self):
        with open(self.state, encoding="utf-8") as f:
            return json.load(f)


class TestScan(Base):
    def _project(self):
        write(os.path.join(self.d, "src/a.py"), "x = 1\ny = 2\n")
        write(os.path.join(self.d, "src/b.py"), "def f():\n    return 3\n")
        self.save({"meta": {}, "bands": [], "spine": [], "modules": [
            {"id": "m_a", "label": "A", "band": "b", "coupling": "low", "deps": [], "paths": ["src/a.py"]},
            {"id": "m_b", "label": "B", "band": "b", "coupling": "low", "deps": [], "paths": ["src/b.py"]},
            {"id": "m_empty", "label": "E", "band": "b", "coupling": "low", "deps": [], "paths": ["nope/**/*.py"]},
        ]})

    def test_loc_hash_empty(self):
        self._project()
        r = run("scan.py", "--root", self.d, "--state", self.state, "--write")
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = json.loads(r.stdout)
        self.assertIn("m_empty", rep["empty"])
        self.assertIn("m_a", rep["unaudited"])
        st = {m["id"]: m for m in self.load()["modules"]}
        self.assertEqual(st["m_a"]["loc"], 2)
        self.assertTrue(st["m_a"]["contentHash"])
        # hash is content-stable: re-scan gives the same hash
        run("scan.py", "--root", self.d, "--state", self.state, "--write")
        self.assertEqual(self.load()["modules"][0]["contentHash"], st["m_a"]["contentHash"])

    def test_stale_detection(self):
        self._project()
        run("scan.py", "--root", self.d, "--state", self.state, "--write")
        st = self.load()
        for m in st["modules"]:
            if m["id"] == "m_a":
                m["auditedHash"] = m["contentHash"]
                m["score"] = 80
        self.save(st)
        # unchanged → fresh
        rep = json.loads(run("scan.py", "--root", self.d, "--state", self.state).stdout)
        self.assertIn("m_a", rep["fresh"])
        # change the file → stale
        write(os.path.join(self.d, "src/a.py"), "x = 1\ny = 2\nz = 3\n")
        rep = json.loads(run("scan.py", "--root", self.d, "--state", self.state).stdout)
        self.assertIn("m_a", rep["stale"])

    def test_git_changed_modules(self):
        if shutil.which("git") is None:
            self.skipTest("git not available")
        self._project()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        g = lambda *a: subprocess.run(["git", "-C", self.d, *a], capture_output=True, text=True, env=env)
        g("init", "-q")
        g("add", "-A"); g("commit", "-qm", "init")
        head = g("rev-parse", "HEAD").stdout.strip()
        st = self.load(); st["meta"]["rev"] = head; self.save(st)
        write(os.path.join(self.d, "src/b.py"), "def f():\n    return 99\n")
        g("add", "-A"); g("commit", "-qm", "change b")
        rep = json.loads(run("scan.py", "--root", self.d, "--state", self.state).stdout)
        self.assertIsNotNone(rep["git"])
        self.assertIn("m_b", rep["git"]["changed_modules"])
        self.assertNotIn("m_a", rep["git"]["changed_modules"])

    def test_codemap_never_audits_itself(self):
        write(os.path.join(self.d, "src/a.py"), "x = 1\n")
        write(os.path.join(self.d, ".codemap/generated.py"), "should_not_count = True\n")
        self.save({"meta": {}, "excludes": [], "bands": [], "spine": [], "modules": [
            {"id": "all_py", "label": "All", "band": "b", "coupling": "low",
             "deps": [], "paths": ["**/*.py"]},
        ]})
        r = run("scan.py", "--root", self.d, "--state", self.state, "--write")
        self.assertEqual(r.returncode, 0, r.stderr)
        mod = self.load()["modules"][0]
        self.assertEqual(mod["loc"], 1)


class TestQuery(Base):
    def _state(self):
        self.save({"meta": {}, "modules": [
            {"id": "good", "label": "G", "band": "x", "coupling": "low", "score": 92, "grade": "A", "tags": ["clean"], "findings": [], "paths": []},
            {"id": "df", "label": "D", "band": "x", "coupling": "low", "score": 70, "grade": "C", "tags": ["dual-format"],
             "findings": [{"sev": "MED", "loc": "a:1", "text": "x"}], "paths": []},
            {"id": "bad", "label": "B", "band": "x", "coupling": "low", "score": 48, "grade": "D", "tags": ["stub"],
             "findings": [{"sev": "HIGH", "loc": "b:2", "text": "y"}], "paths": []},
            {"id": "new", "label": "N", "band": "x", "coupling": "low", "paths": []},
        ]})

    def test_max_grade(self):
        self._state()
        ids = run("query.py", "--state", self.state, "--max-grade", "C", "--format", "ids").stdout.split()
        self.assertCountEqual(ids, ["df", "bad"])

    def test_tag(self):
        self._state()
        ids = run("query.py", "--state", self.state, "--tag", "dual-format", "--format", "ids").stdout.split()
        self.assertEqual(ids, ["df"])

    def test_sev(self):
        self._state()
        ids = run("query.py", "--state", self.state, "--sev", "HIGH", "--format", "ids").stdout.split()
        self.assertEqual(ids, ["bad"])

    def test_needs_audit(self):
        self._state()
        ids = run("query.py", "--state", self.state, "--needs-audit", "--format", "ids").stdout.split()
        self.assertIn("new", ids)


class TestApplyAudit(Base):
    def _state(self):
        self.save({"meta": {}, "modules": [
            {"id": "m1", "label": "M", "band": "x", "coupling": "low", "contentHash": "abc", "paths": []},
        ]})

    def apply(self, result):
        return run("apply_audit.py", "--state", self.state, "--id", "m1", "--json", json.dumps(result))

    def test_valid(self):
        self._state()
        r = self.apply({"score": 70, "grade": "C", "tags": ["legacy"],
                        "findings": [{"sev": "LOW", "loc": "f:1", "text": "t"}]})
        self.assertEqual(r.returncode, 0, r.stderr)
        m = self.load()["modules"][0]
        self.assertEqual(m["score"], 70)
        self.assertEqual(m["auditedHash"], "abc")

    def test_grade_score_mismatch(self):
        self._state()
        r = self.apply({"score": 70, "grade": "A", "tags": ["clean"], "findings": []})
        self.assertNotEqual(r.returncode, 0)

    def test_unknown_tag(self):
        self._state()
        r = self.apply({"score": 70, "grade": "C", "tags": ["not-a-real-tag"],
                        "findings": [{"sev": "LOW", "loc": "f:1", "text": "t"}]})
        self.assertNotEqual(r.returncode, 0)

    def test_clean_with_bad_tag(self):
        self._state()
        r = self.apply({"score": 90, "grade": "A", "tags": ["clean", "legacy"],
                        "findings": [{"sev": "LOW", "loc": "f:1", "text": "t"}]})
        self.assertNotEqual(r.returncode, 0)

    def test_clean_low_score(self):
        self._state()
        r = self.apply({"score": 50, "grade": "D", "tags": ["clean"], "findings": []})
        self.assertNotEqual(r.returncode, 0)

    def test_finding_missing_text(self):
        self._state()
        r = self.apply({"score": 70, "grade": "C", "tags": ["legacy"],
                        "findings": [{"sev": "LOW", "loc": "f:1", "text": ""}]})
        self.assertNotEqual(r.returncode, 0)

    def test_bad_tag_without_findings(self):
        self._state()
        r = self.apply({"score": 70, "grade": "C", "tags": ["legacy"], "findings": []})
        self.assertNotEqual(r.returncode, 0)


class TestVersioning(Base):
    def _prepare(self, project_type="game"):
        codemap = os.path.join(self.d, ".codemap")
        os.makedirs(codemap, exist_ok=True)
        self.state = os.path.join(codemap, "modules.json")
        if project_type == "game":
            write(os.path.join(self.d, "project.godot"), "[application]\nconfig/name=\"Demo\"\n")
        else:
            write(os.path.join(self.d, "package.json"), json.dumps({"dependencies": {"react": "1"}}))
        write(os.path.join(self.d, "src/a.py"), "x = 1\n")
        dimensions, lenses = complete_architecture()
        self.save({
            "meta": {"project": "Demo", "htmlPath": ".codemap/codemap.html",
                     "mdPath": ".codemap/codemap.md"},
            "bands": [{"id": "b", "t": "B"}], "spine": [],
            "architectureDimensions": dimensions,
            "architectureLenses": lenses,
            "modules": [{"id": "m_a", "label": "A", "band": "b", "coupling": "low",
                         "deps": [], "paths": ["src/a.py"]}],
        })
        r = run("scan.py", "--root", self.d, "--state", self.state, "--write")
        self.assertEqual(r.returncode, 0, r.stderr)
        state = self.load()
        mod = state["modules"][0]
        mod.update({"auditedHash": mod["contentHash"], "score": 92, "grade": "A",
                    "tags": ["clean"], "findings": []})
        self.save(state)
        write(os.path.join(codemap, "codemap.html"), "<html>map</html>")
        write(os.path.join(codemap, "codemap.md"), "# map\n")
        write(os.path.join(codemap, "config.json"), "{}\n")

    def _publish(self, *extra):
        return run("version.py", "publish", "--root", self.d, *extra)

    def _audit_current_source(self):
        r = run("scan.py", "--root", self.d, "--state", self.state, "--write")
        self.assertEqual(r.returncode, 0, r.stderr)
        state = self.load()
        for mod in state["modules"]:
            mod["auditedHash"] = mod["contentHash"]
            mod.setdefault("score", 90)
            mod.setdefault("grade", "A")
            mod.setdefault("tags", ["clean"])
            mod.setdefault("findings", [])
        self.save(state)

    def test_full_then_incremental_versions_are_immutable(self):
        self._prepare("game")
        first = self._publish("--mode", "full")
        self.assertEqual(first.returncode, 0, first.stderr)
        first_result = json.loads(first.stdout)
        self.assertEqual(first_result["version"], "audit-v0001")
        v1_manifest = os.path.join(self.d, ".codemap/versions/audit-v0001/manifest.json")
        with open(v1_manifest, "rb") as f:
            v1_hash = f.read()

        write(os.path.join(self.d, "src/a.py"), "x = 2\n")
        self._audit_current_source()
        state = self.load()
        state["modules"][0].update({
            "score": 70, "grade": "C", "tags": ["over-fit"],
            "findings": [{"sev": "MED", "loc": "src/a.py:1",
                          "text": "fixture became specialized"}],
        })
        self.save(state)
        write(os.path.join(self.d, ".codemap/codemap.md"), "# map updated\n")
        second = self._publish("--mode", "incremental")
        self.assertEqual(second.returncode, 0, second.stderr)
        second_result = json.loads(second.stdout)
        self.assertEqual(second_result["version"], "audit-v0002")
        self.assertIn("m_a", second_result["changedModules"])
        self.assertEqual(self.load()["auditDelta"]["newIssues"], 1)
        with open(v1_manifest, "rb") as f:
            self.assertEqual(f.read(), v1_hash)

        verify = run("version.py", "verify", "--root", self.d)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertTrue(json.loads(verify.stdout)["valid"])

        no_delta = self._publish()
        self.assertEqual(no_delta.returncode, 0, no_delta.stderr)
        self.assertEqual(json.loads(no_delta.stdout)["status"], "NO_DELTA")
        with open(os.path.join(self.d, ".codemap/versions/index.json"), encoding="utf-8") as f:
            index = json.load(f)
        self.assertEqual(len(index["versions"]), 2)

        with open(os.path.join(self.d, ".codemap/versions/audit-v0001/codemap.md"),
                  "a", encoding="utf-8") as f:
            f.write("tamper\n")
        tampered = run("version.py", "verify", "--root", self.d)
        self.assertNotEqual(tampered.returncode, 0)
        self.assertIn("artifact hash mismatch", tampered.stdout)

    def test_status_without_retained_baseline_requires_full_audit(self):
        self._prepare("game")
        status = run("version.py", "status", "--root", self.d)
        self.assertEqual(status.returncode, 0, status.stderr)
        result = json.loads(status.stdout)
        self.assertEqual(result["status"], "NEEDS_FULL_AUDIT")
        self.assertIsNone(result.get("latestVersion"))

    def test_first_publish_stamps_versioned_state_and_projection(self):
        self._prepare("game")
        published = self._publish("--mode", "full", "--expected-baseline", "none",
                                  "--gate", "unit-tests:pass")
        self.assertEqual(published.returncode, 0, published.stderr)
        live = self.load()
        self.assertEqual(live["auditVersion"]["version"], "audit-v0001")
        self.assertEqual(live["auditVersion"]["mode"], "full")
        self.assertIsNone(live["auditVersion"]["baseline"])
        self.assertIsNone(live["auditDelta"])
        with open(os.path.join(self.d, ".codemap/codemap.html"), encoding="utf-8") as f:
            self.assertIn("audit-v0001", f.read())
        with open(os.path.join(self.d, ".codemap/versions/audit-v0001/manifest.json"),
                  encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["validationGates"], ["unit-tests:pass"])
        self.assertIn("m_a", manifest["scope"])
        self.assertIn("dimension:safeguards", manifest["scope"])

    def test_incomplete_eight_dimension_contract_is_blocked(self):
        self._prepare("game")
        state = self.load()
        state["architectureDimensions"].pop()
        self.save(state)
        result = self._publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("eight canonical", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.d, ".codemap/versions/index.json")))

    def test_expected_baseline_mismatch_fails_closed(self):
        self._prepare("game")
        result = self._publish("--expected-baseline", "audit-v9999")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected baseline", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.d, ".codemap/versions/index.json")))

    def test_status_expands_changed_module_to_direct_consumers(self):
        self._prepare("game")
        write(os.path.join(self.d, "src/b.py"), "from_a = 1\n")
        state = self.load()
        state["modules"].append({"id": "m_b", "label": "B", "band": "b",
                                 "coupling": "med", "deps": ["m_a"],
                                 "paths": ["src/b.py"]})
        self.save(state)
        self._audit_current_source()
        self.assertEqual(self._publish().returncode, 0)
        write(os.path.join(self.d, "src/a.py"), "x = 2\n")
        status = run("version.py", "status", "--root", self.d)
        self.assertEqual(status.returncode, 0, status.stderr)
        result = json.loads(status.stdout)
        self.assertIn("m_a", result["affectedModules"]["changedModules"])
        self.assertIn("m_b", result["affectedModules"]["directConsumers"])
        self.assertIn("m_b", result["affectedModules"]["suggestedAuditScope"])

    def test_rollback_restores_verified_working_view_and_keeps_history(self):
        self._prepare("game")
        self.assertEqual(self._publish().returncode, 0)
        v1_state = os.path.join(self.d, ".codemap/versions/audit-v0001/modules.json")
        with open(v1_state, "rb") as f:
            v1_bytes = f.read()
        write(os.path.join(self.d, "src/a.py"), "x = 2\n")
        self._audit_current_source()
        self.assertEqual(self._publish("--mode", "incremental").returncode, 0)
        rolled = run("version.py", "rollback", "--root", self.d,
                     "--to", "audit-v0001", "--reason", "test recovery")
        self.assertEqual(rolled.returncode, 0, rolled.stderr)
        with open(self.state, "rb") as f:
            self.assertEqual(f.read(), v1_bytes)
        with open(os.path.join(self.d, ".codemap/versions/index.json"), encoding="utf-8") as f:
            index = json.load(f)
        self.assertEqual(index["latestVersion"], "audit-v0002")
        self.assertEqual(index["activeVersion"], "audit-v0001")
        self.assertEqual(index["rollbacks"][-1]["reason"], "test recovery")
        self.assertEqual(len(index["versions"]), 2)
        verified = run("version.py", "verify", "--root", self.d)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertTrue(json.loads(verified.stdout)["workingAuditMatchesActive"])

    def test_existing_writer_lock_is_not_removed(self):
        self._prepare("game")
        lock = os.path.join(self.d, ".codemap/version.lock")
        write(lock, '{"token":"other","pid":123}')
        result = self._publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another audit version writer", result.stderr)
        self.assertTrue(os.path.isfile(lock))

    def test_nested_godot_project_is_detected_as_game(self):
        self._prepare("game")
        os.remove(os.path.join(self.d, "project.godot"))
        write(os.path.join(self.d, "game/project.godot"),
              "[application]\nconfig/name=\"Nested Demo\"\n")
        status = run("version.py", "status", "--root", self.d)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["projectType"], "game")

    def test_dimension_only_change_creates_successor_version(self):
        self._prepare("game")
        first = self._publish("--mode", "full")
        self.assertEqual(first.returncode, 0, first.stderr)
        state = self.load()
        state["architectureDimensions"][0]["summary"] = "new responsibility evidence"
        state["architectureLenses"][0]["summary"] = "new lens summary"
        self.save(state)
        second = self._publish("--mode", "incremental")
        self.assertEqual(second.returncode, 0, second.stderr)
        result = json.loads(second.stdout)
        self.assertEqual(result["version"], "audit-v0002")
        self.assertEqual(result["changedModules"], [])
        with open(os.path.join(self.d, ".codemap/versions/audit-v0002/delta.json"),
                  encoding="utf-8") as f:
            delta = json.load(f)
        self.assertEqual(delta["architecture"]["dimensions"]["changed"], ["responsibility"])
        self.assertEqual(delta["architecture"]["lenses"]["changed"], ["split"])

    def test_website_detection_and_codemap_output_exclusion(self):
        self._prepare("website")
        first = self._publish()
        self.assertEqual(first.returncode, 0, first.stderr)
        with open(os.path.join(self.d, ".codemap/versions/audit-v0001/manifest.json"),
                  encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["projectType"], "website")
        write(os.path.join(self.d, ".codemap/local-note.txt"), "audit output only")
        status = run("version.py", "status", "--root", self.d)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["status"], "UP_TO_DATE")

    def test_stale_module_cannot_be_published(self):
        self._prepare("game")
        self.assertEqual(self._publish().returncode, 0)
        write(os.path.join(self.d, "src/a.py"), "x = 99\n")
        r = self._publish()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("stale/unaudited", r.stderr)
        with open(os.path.join(self.d, ".codemap/versions/index.json"), encoding="utf-8") as f:
            index = json.load(f)
        self.assertEqual(index["latestVersion"], "audit-v0001")
        self.assertEqual(len(index["versions"]), 1)
        self.assertFalse(os.path.exists(os.path.join(self.d, ".codemap/version.lock")))


class TestRender(Base):
    def _render(self, state):
        self.save(state)
        out_html = os.path.join(self.d, "out.html")
        out_md = os.path.join(self.d, "out.md")
        r = run("render.py", "--state", self.state, "--template", TEMPLATE,
                "--out-html", out_html, "--out-md", out_md)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(out_html, encoding="utf-8") as f:
            html = f.read()
        with open(out_md, encoding="utf-8") as f:
            md = f.read()
        return html, md

    def test_basic(self):
        html, md = self._render({"meta": {"project": "Demo"}, "bands": [{"id": "b", "t": "B"}], "spine": [],
            "modules": [{"id": "m1", "label": "Widget", "band": "b", "coupling": "low", "deps": [],
                         "loc": 5, "score": 80, "grade": "B", "tags": ["clean"], "findings": []}]})
        self.assertIn("Widget", html)        # label present in the DATA
        self.assertIn("function esc(", html)  # the escaper ships
        self.assertIn("Widget", md)

    def test_script_breakout_blocked(self):
        # a label containing </script> must not be able to close the data <script> tag
        html, _ = self._render({"meta": {}, "bands": [{"id": "b", "t": "B"}], "spine": [],
            "modules": [{"id": "m1", "label": "</script><script>alert(1)</script>", "band": "b",
                         "coupling": "low", "deps": [], "loc": 1, "score": 50, "grade": "D",
                         "tags": ["stub"], "findings": [{"sev": "HIGH", "loc": "a:1", "text": "x"}]}]})
        self.assertEqual(html.count("</script>"), 1)  # only the real closing tag

    def test_four_lenses_and_eight_dimensions_render(self):
        ids = ["responsibility", "boundary", "contract", "dependency", "data_logic",
               "composition_state", "evolution", "safeguards"]
        dims = []
        for i, dim_id in enumerate(ids):
            item = {"id": dim_id, "status": "good" if i else "warning",
                    "summary": f"summary-{dim_id}", "relatedModules": ["m1"]}
            if i == 0:
                item["evidence"] = [{"type": "code", "file": "src/a.py",
                                     "description": "one owner is too broad"}]
            dims.append(item)
        state = {"meta": {"project": "Demo", "lang": "zh"},
                 "bands": [{"id": "b", "t": "B"}], "spine": [],
                 "architectureDimensions": dims,
                 "architectureLenses": [
                     {"id": "split", "summary": "先看职责和边界"},
                     {"id": "connect", "summary": "再看接口和依赖"},
                     {"id": "change", "summary": "再看数据组合迁移"},
                     {"id": "protect", "summary": "最后看保障"},
                 ],
                 "auditVersion": {"version": None, "mode": "working",
                                  "trigger": "test", "scope": ["m1"]},
                 "modules": [{"id": "m1", "label": "Widget", "band": "b",
                              "coupling": "low", "deps": [], "loc": 5,
                              "score": 80, "grade": "B", "tags": ["clean"],
                              "findings": []}]}
        html, md = self._render(state)
        self.assertIn('id="lensApp"', html)
        self.assertIn("每个模块是不是只管自己的事？", html)
        self.assertIn("architectureDimensions", html)
        self.assertIn("当前工作视图 · 尚未留版", html)
        self.assertIn("## 分 · 连 · 变 · 保", md)
        for label in ("模块职责", "模块边界", "Contract / 接口", "依赖方向",
                      "数据与逻辑分离", "状态与组合", "长期演进", "工程保障"):
            self.assertIn(label, md)

    def test_warning_or_risk_requires_evidence(self):
        self.save({"meta": {}, "modules": [{"id": "m1", "label": "M"}],
                   "architectureDimensions": [
                       {"id": "responsibility", "status": "risk",
                        "summary": "too broad", "relatedModules": ["m1"]}]})
        r = run("render.py", "--state", self.state, "--template", TEMPLATE,
                "--out-html", os.path.join(self.d, "out.html"),
                "--out-md", os.path.join(self.d, "out.md"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("requires evidence", r.stderr)

    def test_unknown_module_reference_is_rejected(self):
        self.save({"meta": {}, "modules": [{"id": "m1", "label": "M"}],
                   "architectureDimensions": [
                       {"id": "contract", "status": "good", "summary": "ok",
                        "relatedModules": ["missing"]}]})
        r = run("render.py", "--state", self.state, "--template", TEMPLATE,
                "--out-html", os.path.join(self.d, "out.html"),
                "--out-md", os.path.join(self.d, "out.md"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown module", r.stderr)

    def test_legacy_state_renders_explicit_unknown(self):
        html, md = self._render({"meta": {"project": "Demo", "lang": "zh"},
            "bands": [{"id": "b", "t": "B"}], "spine": [],
            "modules": [{"id": "m1", "label": "Widget", "band": "b",
                         "coupling": "low", "deps": [], "loc": 5,
                         "score": 80, "grade": "B", "tags": ["clean"],
                         "findings": []}]})
        self.assertIn("尚未完成结构化审计", html)
        self.assertIn("unknown", md)


if __name__ == "__main__":
    unittest.main()
