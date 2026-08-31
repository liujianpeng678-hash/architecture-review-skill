#!/usr/bin/env python3
"""Immutable version snapshots for architecture-review.

The live ``.codemap/modules.json`` remains the mutable source of truth.  This tool
publishes immutable, hash-chained audit snapshots under
``.codemap/versions/audit-vNNNN`` and detects project drift without requiring Git.

Commands:
  status  --root <project>
  publish --root <project> [--mode full|incremental|expanded_incremental]
  verify  --root <project>

Stdlib only; Python 3.9+.
"""

import argparse
import contextlib
import datetime
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

from audit_contract import (
    AuditContract,
    AuditContractError,
    AuditSchemaRegistry,
    CANONICAL_DIMENSIONS,
    CANONICAL_LENSES,
    STATE_SCHEMA_VERSION,
    format_contract_error,
)

SCHEMA_VERSION = STATE_SCHEMA_VERSION
TOOL_VERSION = "2.1"
VERSION_RE = re.compile(r"^audit-v(\d{4})$")
MODES = {"full", "incremental", "expanded_incremental"}

SOURCE_EXCLUDES = (
    "/.codemap/", "/.git/", "/.svn/", "/.hg/", "/.idea/", "/.vs/",
    "/.codegraph/", "/.codex-tmp/", "/.codex-local-history/",
    "/node_modules/", "/vendor/", "/third_party/", "/external/",
    "/.venv/", "/venv/", "/__pycache__/", "/.pytest_cache/",
    "/dist/", "/build/", "/out/", "/output/", "/target/", "/bin/",
    "/obj/", "/coverage/", "/.next/", "/.nuxt/", "/.godot/",
    "/library/", "/temp/", "/logs/", "/qa_screenshots/",
)
SOURCE_FILE_EXCLUDES = (
    ".pyc", ".pyo", ".tmp", ".temp", ".log", ".cache", ".swp", ".swo",
    ".ds_store", "thumbs.db",
)
SENSITIVE_NAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "credentials.json", "service-account.json", "id_rsa", "id_ed25519",
}
SENSITIVE_SUFFIXES = {".pem", ".p12", ".pfx", ".key"}


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def fail(message):
    raise SystemExit("version: ERROR — " + message)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, value):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def write_json_atomic(path, value):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-index-", suffix=".json", dir=os.path.dirname(path))
    os.close(fd)
    try:
        write_json(tmp, value)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_json_hash(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def architecture_state_view(state):
    """Drop volatile scan/render metadata before deciding whether an audit changed."""
    meta = dict(state.get("meta") or {})
    for key in ("generatedAt", "rev", "tracked_loc", "tracked_files", "locLine"):
        meta.pop(key, None)
    return {
        "meta": meta,
        "excludes": state.get("excludes", []),
        "bands": state.get("bands", []),
        "spine": state.get("spine", []),
        "reportThemes": state.get("reportThemes", []),
        "architectureLenses": state.get("architectureLenses", []),
        "architectureDimensions": state.get("architectureDimensions", []),
        "modules": state.get("modules", []),
    }


def normalize_rel(path):
    value = str(path).replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value.lstrip("/")


def is_sensitive(name):
    low = name.lower()
    return low in SENSITIVE_NAMES or any(low.endswith(s) for s in SENSITIVE_SUFFIXES)


def path_excluded(rel, is_dir=False, exact_excludes=None):
    rel = normalize_rel(rel)
    low = "/" + rel.lower().strip("/") + ("/" if is_dir else "")
    if any(token in low for token in SOURCE_EXCLUDES):
        return True
    if not is_dir and any(low.endswith(token) for token in SOURCE_FILE_EXCLUDES):
        return True
    return rel in (exact_excludes or set())


def snapshot_fingerprint(entries):
    rows = ["{}:{}:{}".format(e["path"], e["size"], e["sha256"]) for e in entries]
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def collect_source_snapshot(root, previous=None, exact_excludes=None):
    root = os.path.realpath(root)
    previous_by_path = {e["path"]: e for e in (previous or {}).get("files", [])}
    exact_excludes = {normalize_rel(p) for p in (exact_excludes or set())}
    entries = []
    sensitive_skipped = []
    unreadable = []

    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        kept = []
        for d in dirs:
            full = os.path.join(current, d)
            rel = normalize_rel(os.path.relpath(full, root))
            if os.path.islink(full) or path_excluded(rel, True, exact_excludes):
                continue
            kept.append(d)
        dirs[:] = kept

        for name in files:
            full = os.path.join(current, name)
            rel = normalize_rel(os.path.relpath(full, root))
            if os.path.islink(full) or path_excluded(rel, False, exact_excludes):
                continue
            if is_sensitive(name):
                sensitive_skipped.append(rel)
                continue
            try:
                st = os.stat(full)
                old = previous_by_path.get(rel)
                if old and old.get("size") == st.st_size and old.get("mtimeNs") == st.st_mtime_ns:
                    digest = old["sha256"]
                else:
                    digest = sha256_file(full)
                entries.append({
                    "path": rel,
                    "size": st.st_size,
                    "mtimeNs": st.st_mtime_ns,
                    "sha256": digest,
                })
            except OSError as exc:
                unreadable.append({"path": rel, "error": str(exc)})

    entries.sort(key=lambda e: e["path"])
    return {
        "schemaVersion": SCHEMA_VERSION,
        "createdAt": now_iso(),
        "fileCount": len(entries),
        "totalBytes": sum(e["size"] for e in entries),
        "sourceFingerprint": snapshot_fingerprint(entries),
        "sensitiveSkipped": sorted(sensitive_skipped),
        "unreadable": unreadable,
        "files": entries,
    }


def snapshot_delta(previous, current):
    old = {e["path"]: e for e in (previous or {}).get("files", [])}
    new = {e["path"]: e for e in current.get("files", [])}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    modified = sorted(p for p in set(old) & set(new) if old[p]["sha256"] != new[p]["sha256"])
    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "counts": {
            "added": len(added),
            "removed": len(removed),
            "modified": len(modified),
            "unchanged": len(set(old) & set(new)) - len(modified),
        },
    }


def detect_project_type(root):
    root_path = Path(root)
    game = (root_path / "project.godot").is_file()
    game = game or (root_path / "game" / "project.godot").is_file()
    game = game or bool(list(root_path.glob("*.uproject")))
    game = game or ((root_path / "Assets").is_dir() and (root_path / "ProjectSettings").is_dir())
    web = (root_path / ".openai" / "hosting.json").is_file()
    web = web or (root_path / "index.html").is_file()
    web = web or any((root_path / n).is_file() for n in (
        "next.config.js", "next.config.mjs", "vite.config.js", "vite.config.ts",
        "astro.config.mjs", "svelte.config.js", "nuxt.config.ts",
    ))

    package = root_path / "package.json"
    if package.is_file():
        try:
            data = read_json(package)
            deps = set((data.get("dependencies") or {})) | set((data.get("devDependencies") or {}))
            web = web or bool(deps & {
                "next", "react", "react-dom", "vue", "svelte", "astro", "vite",
                "@angular/core", "nuxt", "solid-js",
            })
            game = game or bool(deps & {
                "three", "phaser", "pixi.js", "@pixi/core", "babylonjs", "@babylonjs/core",
            })
        except (OSError, ValueError, TypeError):
            pass

    return "hybrid" if web and game else "website" if web else "game" if game else "unknown"


def codemap_paths(root, state_arg=None):
    root = os.path.realpath(root)
    codemap = os.path.join(root, ".codemap")
    state_path = os.path.abspath(state_arg) if state_arg else os.path.join(codemap, "modules.json")
    return root, codemap, state_path


def output_paths(root, state_path, state):
    meta = state.get("meta") or {}

    def resolve(value, default):
        value = value or default
        return value if os.path.isabs(value) else os.path.join(root, value)

    return (
        os.path.abspath(resolve(meta.get("htmlPath"), ".codemap/codemap.html")),
        os.path.abspath(resolve(meta.get("mdPath"), ".codemap/codemap.md")),
    )


def rel_if_inside(path, root):
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    except ValueError:
        return None
    if rel == ".." or rel.startswith(".." + os.sep):
        return None
    return normalize_rel(rel)


def exact_audit_outputs(root, state_path, html_path, md_path):
    out = set()
    for p in (state_path, html_path, md_path):
        rel = rel_if_inside(p, root)
        if rel:
            out.add(rel)
    return out


def load_index(versions_root):
    path = os.path.join(versions_root, "index.json")
    if not os.path.isfile(path):
        return {"schemaVersion": SCHEMA_VERSION, "latestVersion": None,
                "activeVersion": None, "versions": [], "rollbacks": []}
    data = read_json(path)
    try:
        schema = AuditSchemaRegistry().detect_schema(data)
    except AuditContractError as exc:
        fail(format_contract_error(exc))
    if schema != SCHEMA_VERSION:
        fail("unsupported versions/index.json schemaVersion: {}".format(schema))
    if not isinstance(data.get("versions"), list):
        fail("invalid versions/index.json: versions must be an array")
    data["schemaVersion"] = SCHEMA_VERSION
    data.setdefault("activeVersion", data.get("latestVersion"))
    data.setdefault("rollbacks", [])
    return data


def latest_paths(versions_root, index):
    version = index.get("latestVersion")
    if not version:
        return None, None, None
    base = os.path.join(versions_root, version)
    return base, os.path.join(base, "manifest.json"), os.path.join(base, "source-snapshot.json")


def next_version(versions_root, index):
    nums = []
    for item in index.get("versions", []):
        match = VERSION_RE.match(str(item.get("version", "")))
        if match:
            nums.append(int(match.group(1)))
    if os.path.isdir(versions_root):
        for name in os.listdir(versions_root):
            match = VERSION_RE.match(name)
            if match:
                nums.append(int(match.group(1)))
    return "audit-v{:04d}".format((max(nums) if nums else 0) + 1)


def run_scan(root, state_path):
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan.py")
    proc = subprocess.run(
        [sys.executable, script, "--root", root, "--state", state_path],
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        fail("scan.py failed: " + (proc.stderr.strip() or proc.stdout.strip()))
    try:
        return json.loads(proc.stdout)
    except ValueError:
        fail("scan.py returned invalid JSON")


def module_map(state):
    return {m.get("id"): m for m in state.get("modules", []) if m.get("id")}


def module_delta(previous_state, current_state):
    old = module_map(previous_state or {})
    new = module_map(current_state)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    content_changed = sorted(
        mid for mid in set(old) & set(new)
        if old[mid].get("contentHash") != new[mid].get("contentHash")
    )
    audit_changed = sorted(
        mid for mid in set(old) & set(new)
        if any(old[mid].get(k) != new[mid].get(k)
               for k in ("score", "grade", "tags", "findings", "auditedHash"))
    )
    return {
        "added": added,
        "removed": removed,
        "contentChanged": content_changed,
        "auditChanged": audit_changed,
    }


def keyed_state_delta(previous_state, current_state, key):
    old = {str(item.get("id")): item for item in (previous_state or {}).get(key, []) if item.get("id")}
    new = {str(item.get("id")): item for item in (current_state or {}).get(key, []) if item.get("id")}
    return {
        "added": sorted(set(new) - set(old)),
        "removed": sorted(set(old) - set(new)),
        "changed": sorted(
            item_id for item_id in set(old) & set(new)
            if stable_json_hash(old[item_id]) != stable_json_hash(new[item_id])
        ),
    }


def architecture_delta(previous_state, current_state):
    return {
        "dimensions": keyed_state_delta(previous_state, current_state, "architectureDimensions"),
        "lenses": keyed_state_delta(previous_state, current_state, "architectureLenses"),
    }


def architecture_delta_count(delta):
    return sum(len(values) for group in delta.values() for values in group.values())


def validate_complete_audit_state(state, contract):
    """Validate every publishable semantic fact through the shared contract."""
    try:
        contract.validate_publishable_state(state)
    except AuditContractError as exc:
        fail(format_contract_error(exc))


def reverse_dependency_scope(state, changed_ids):
    """Return direct and transitive consumers for incremental-audit scoping."""
    modules = module_map(state)
    changed = sorted(mid for mid in set(changed_ids) if mid in modules)
    reverse = {mid: set() for mid in modules}
    for mid, mod in modules.items():
        for dep in mod.get("deps") or []:
            if dep in reverse:
                reverse[dep].add(mid)
    direct = sorted({consumer for mid in changed for consumer in reverse.get(mid, set())}
                    - set(changed))
    seen = set(changed)
    queue = list(changed)
    while queue:
        current = queue.pop(0)
        for consumer in sorted(reverse.get(current, set())):
            if consumer not in seen:
                seen.add(consumer)
                queue.append(consumer)
    transitive = sorted(seen - set(changed) - set(direct))
    return {
        "changedModules": changed,
        "directConsumers": direct,
        "transitiveConsumers": transitive,
        "suggestedAuditScope": changed + direct,
    }


def issue_records(state):
    """Stable issue identities for human-facing incremental summaries."""
    records = {}
    for mod in state.get("modules") or []:
        mid = mod.get("id")
        for finding in mod.get("findings") or []:
            record = {
                "kind": "module-finding",
                "moduleId": mid,
                "severity": finding.get("sev"),
                "location": finding.get("loc"),
                "text": finding.get("text"),
            }
            key = stable_json_hash(record)
            records[key] = record
    for dim in state.get("architectureDimensions") or []:
        if dim.get("status") not in {"warning", "risk"}:
            continue
        record = {
            "kind": "architecture-dimension",
            "dimensionId": dim.get("id"),
            "status": dim.get("status"),
            "summary": dim.get("summary"),
        }
        key = "dimension:" + str(dim.get("id"))
        records[key] = record
    return records


def issue_delta(previous_state, current_state):
    previous = issue_records(previous_state or {})
    current = issue_records(current_state)
    old_keys, new_keys = set(previous), set(current)
    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    persisting = sorted(old_keys & new_keys)
    old_unknown = {d.get("id") for d in (previous_state or {}).get("architectureDimensions", [])
                   if d.get("status") == "unknown"}
    new_unknown = {d.get("id") for d in current_state.get("architectureDimensions", [])
                   if d.get("status") == "unknown"}
    return {
        "newIssues": len(added),
        "resolvedIssues": len(removed),
        "persistingIssues": len(persisting),
        "unknownChanged": len(old_unknown ^ new_unknown),
        "new": [current[key] for key in added],
        "resolved": [previous[key] for key in removed],
        "persisting": [current[key] for key in persisting],
        "unknownDimensions": sorted(new_unknown),
    }


def flatten_values(values):
    result = []
    for value in values or []:
        for item in str(value).split(","):
            item = item.strip()
            if item and item not in result:
                result.append(item)
    return result


def glob_matches(path, pattern):
    path = normalize_rel(path)
    pattern = normalize_rel(pattern)
    return (fnmatch.fnmatchcase(path, pattern)
            or PurePosixPath(path).match(pattern)
            or (pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])))


def map_changed_files_to_modules(changed_files, state):
    mapped = {}
    unmapped = []
    modules = state.get("modules", [])
    for path in changed_files:
        owners = []
        for mod in modules:
            patterns = mod.get("paths") or []
            if isinstance(patterns, str):
                patterns = [patterns]
            if any(glob_matches(path, pat) for pat in patterns):
                owners.append(mod.get("id"))
        if owners:
            mapped[path] = owners
        else:
            unmapped.append(path)
    return mapped, sorted(unmapped)


def score_summary(state):
    modules = state.get("modules", [])
    scored = [m for m in modules if m.get("score") is not None]
    grades = {g: 0 for g in ("A", "B", "C", "D", "F")}
    severities = {s: 0 for s in ("HIGH", "MED", "LOW")}
    for mod in scored:
        grade = mod.get("grade")
        if grade in grades:
            grades[grade] += 1
        for finding in mod.get("findings") or []:
            sev = finding.get("sev")
            if sev in severities:
                severities[sev] += 1
    average = round(sum(m["score"] for m in scored) / len(scored), 2) if scored else None
    return {
        "moduleCount": len(modules),
        "scoredModules": len(scored),
        "averageScore": average,
        "gradeCounts": grades,
        "findingCounts": severities,
    }


@contextlib.contextmanager
def version_lock(codemap):
    os.makedirs(codemap, exist_ok=True)
    path = os.path.join(codemap, "version.lock")
    token = str(uuid.uuid4())
    payload = {"token": token, "pid": os.getpid(), "createdAt": now_iso()}
    try:
        with open(path, "x", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except FileExistsError:
        fail("another audit version writer holds .codemap/version.lock")
    try:
        yield
    finally:
        try:
            current = read_json(path)
            if current.get("token") == token:
                os.unlink(path)
        except (OSError, ValueError, TypeError):
            pass


def current_context(root, codemap, state_path):
    if not os.path.isfile(state_path):
        return {"needsInit": True, "projectType": detect_project_type(root)}
    state = read_json(state_path)
    html_path, md_path = output_paths(root, state_path, state)
    exact = exact_audit_outputs(root, state_path, html_path, md_path)
    versions_root = os.path.join(codemap, "versions")
    index = load_index(versions_root)
    latest_base, _, latest_snapshot_path = latest_paths(versions_root, index)
    previous_snapshot = read_json(latest_snapshot_path) if latest_snapshot_path and os.path.isfile(latest_snapshot_path) else None
    current_snapshot = collect_source_snapshot(root, previous_snapshot, exact)
    source_delta = snapshot_delta(previous_snapshot, current_snapshot)
    scan_report = run_scan(root, state_path)
    previous_state = None
    if latest_base:
        previous_state_path = os.path.join(latest_base, "modules.json")
        if os.path.isfile(previous_state_path):
            previous_state = read_json(previous_state_path)
    mod_delta = module_delta(previous_state, state)
    arch_delta = architecture_delta(previous_state, state)
    changed_files = source_delta["added"] + source_delta["modified"] + source_delta["removed"]
    mapped, unmapped = map_changed_files_to_modules(changed_files, state)
    changed_module_ids = set(scan_report.get("needs_audit", []))
    changed_module_ids.update(mod_delta.get("added", []))
    changed_module_ids.update(mod_delta.get("contentChanged", []))
    for owners in mapped.values():
        changed_module_ids.update(mid for mid in owners if mid)
    affected = reverse_dependency_scope(state, changed_module_ids)
    return {
        "needsInit": False,
        "projectType": detect_project_type(root),
        "state": state,
        "statePath": state_path,
        "htmlPath": html_path,
        "mdPath": md_path,
        "exactExcludes": exact,
        "versionsRoot": versions_root,
        "index": index,
        "latestBase": latest_base,
        "previousSnapshot": previous_snapshot,
        "currentSnapshot": current_snapshot,
        "sourceDelta": source_delta,
        "scan": scan_report,
        "previousState": previous_state,
        "moduleDelta": mod_delta,
        "architectureDelta": arch_delta,
        "mappedChangedFiles": mapped,
        "unmappedChangedFiles": unmapped,
        "affectedModules": affected,
    }


def cmd_status(args):
    root, codemap, state_path = codemap_paths(args.root, args.state)
    if os.path.isfile(state_path):
        versions_root = os.path.join(codemap, "versions")
        index = load_index(versions_root)
        if not index.get("latestVersion"):
            scan_report = run_scan(root, state_path)
            state = read_json(state_path)
            print(json.dumps({
                "status": "NEEDS_FULL_AUDIT",
                "projectType": detect_project_type(root),
                "statePath": state_path,
                "latestVersion": None,
                "activeVersion": None,
                "needsAudit": scan_report.get("needs_audit", []),
                "emptyModules": scan_report.get("empty", []),
                "affectedModules": reverse_dependency_scope(
                    state, scan_report.get("needs_audit", [])),
            }, ensure_ascii=False, indent=2))
            return
    ctx = current_context(root, codemap, state_path)
    if ctx.get("needsInit"):
        result = {
            "status": "NEEDS_FULL_AUDIT",
            "projectType": ctx["projectType"],
            "statePath": state_path,
        }
    else:
        delta_count = sum(ctx["sourceDelta"]["counts"][k] for k in ("added", "removed", "modified"))
        architecture_changed = architecture_delta_count(ctx["architectureDelta"]) > 0
        result = {
            "status": "UP_TO_DATE" if delta_count == 0 and not architecture_changed and ctx["scan"].get("up_to_date") else "NEEDS_INCREMENTAL_AUDIT",
            "projectType": ctx["projectType"],
            "latestVersion": ctx["index"].get("latestVersion"),
            "activeVersion": ctx["index"].get("activeVersion") or ctx["index"].get("latestVersion"),
            "sourceFingerprint": ctx["currentSnapshot"]["sourceFingerprint"],
            "sourceDelta": ctx["sourceDelta"],
            "needsAudit": ctx["scan"].get("needs_audit", []),
            "emptyModules": ctx["scan"].get("empty", []),
            "moduleDelta": ctx["moduleDelta"],
            "architectureDelta": ctx["architectureDelta"],
            "mappedChangedFiles": ctx["mappedChangedFiles"],
            "unmappedChangedFiles": ctx["unmappedChangedFiles"],
            "affectedModules": ctx["affectedModules"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def effective_standard(state_path):
    project_standard = os.path.join(os.path.dirname(os.path.abspath(state_path)), "standard.json")
    if os.path.isfile(project_standard):
        return project_standard
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "reference", "standard.json"))


def load_audit_contract(state_path):
    path = effective_standard(state_path)
    try:
        return AuditContract.from_path(path), path
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, AuditContractError):
            fail(format_contract_error(exc))
        fail("cannot load effective standard: " + str(exc))


def semantic_manifest_summary(receipt):
    keys = (
        "receiptVersion", "semanticContractVersion", "stateSchemaVersion",
        "compatibilityStatus", "semanticValid", "contractFingerprint",
        "standardFingerprint", "stateFingerprint", "sourceFingerprint",
        "gateFingerprint", "scopeFingerprint", "receiptFingerprint",
    )
    return {key: receipt.get(key) for key in keys}


def copy_artifact(src, dst):
    if not os.path.isfile(src):
        fail("required audit artifact missing: " + src)
    shutil.copy2(src, dst)


def default_template_path():
    return os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "assets", "template.html"))


def render_staged_audit(stage, template_path):
    render_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render.py")
    proc = subprocess.run(
        [sys.executable, render_script,
         "--state", os.path.join(stage, "modules.json"),
         "--template", template_path,
         "--out-html", os.path.join(stage, "codemap.html"),
         "--out-md", os.path.join(stage, "codemap.md"),
         "--standard", os.path.join(stage, "standard.json")],
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        fail("render.py failed while staging: " + (proc.stderr.strip() or proc.stdout.strip()))


def atomic_copy(src, dst):
    """Copy one file through a same-directory temporary and atomic replace."""
    dst = os.path.abspath(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-audit-", dir=os.path.dirname(dst))
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def update_live_artifacts_and_index(codemap, replacements, versions_root, index):
    """Update mutable working artifacts and index as one recoverable transaction."""
    tx = tempfile.mkdtemp(prefix=".publish-transaction-", dir=codemap)
    backups = []
    index_path = os.path.join(versions_root, "index.json")
    try:
        for src, dst in replacements:
            existed = os.path.isfile(dst)
            backup = os.path.join(tx, "backup-{:03d}".format(len(backups)))
            if existed:
                shutil.copy2(dst, backup)
            backups.append((dst, backup, existed))
            atomic_copy(src, dst)
        write_json_atomic(index_path, index)
    except BaseException:
        for dst, backup, existed in reversed(backups):
            try:
                if existed and os.path.isfile(backup):
                    atomic_copy(backup, dst)
                elif not existed and os.path.isfile(dst):
                    os.unlink(dst)
            except OSError:
                pass
        raise
    else:
        shutil.rmtree(tx, ignore_errors=True)


def version_scope(args, state, mode, affected, architecture_change):
    explicit = flatten_values(getattr(args, "scope", []))
    if explicit:
        return explicit
    if mode == "full":
        return sorted(module_map(state)) + ["dimension:" + dim for dim in CANONICAL_DIMENSIONS]
    scope = list(affected.get("suggestedAuditScope") or [])
    for group, values in architecture_change.items():
        for key in ("added", "removed", "changed"):
            for item_id in values.get(key, []):
                token = "{}:{}".format("dimension" if group == "dimensions" else "lens", item_id)
                if token not in scope:
                    scope.append(token)
    return scope


def cmd_publish(args):
    root, codemap, state_path = codemap_paths(args.root, args.state)
    project_type = detect_project_type(root)
    if project_type == "unknown" and not args.allow_unknown:
        fail("project is not recognized as a website or game; use --allow-unknown only for an explicit audit")

    with version_lock(codemap):
        ctx = current_context(root, codemap, state_path)
        if ctx.get("needsInit"):
            fail("modules.json missing; run the full architecture-review init workflow first")

        state = ctx["state"]
        contract, standard_path = load_audit_contract(state_path)
        # Validate persisted audit meaning before interpreting scan freshness. This
        # preserves a stable semantic error for a forged auditedHash instead of
        # misclassifying it as ordinary source staleness.
        validate_complete_audit_state(state, contract)

        scan_report = ctx["scan"]
        if not args.allow_incomplete:
            if scan_report.get("needs_audit_count"):
                fail("stale/unaudited modules remain: " + ", ".join(scan_report.get("needs_audit", [])))
            if scan_report.get("empty"):
                fail("empty module paths remain: " + ", ".join(scan_report.get("empty", [])))
        latest_version = ctx["index"].get("latestVersion")
        if args.expected_baseline is not None:
            expected = args.expected_baseline.strip()
            expected = None if expected.lower() in {"", "none", "null"} else expected
            if expected != latest_version:
                fail("expected baseline {!r}, found {!r}".format(expected, latest_version))

        html_path, md_path = ctx["htmlPath"], ctx["mdPath"]

        previous_state = ctx["previousState"] or {}
        same_source = ctx["sourceDelta"]["counts"]["added"] == 0 \
            and ctx["sourceDelta"]["counts"]["removed"] == 0 \
            and ctx["sourceDelta"]["counts"]["modified"] == 0
        same_architecture = bool(previous_state) and (
            stable_json_hash(architecture_state_view(previous_state))
            == stable_json_hash(architecture_state_view(state))
        )
        if latest_version and same_source and same_architecture and not args.force:
            print(json.dumps({
                "status": "NO_DELTA",
                "latestVersion": latest_version,
                "activeVersion": ctx["index"].get("activeVersion") or latest_version,
                "sourceFingerprint": ctx["currentSnapshot"]["sourceFingerprint"],
            }, ensure_ascii=False, indent=2))
            return

        versions_root = ctx["versionsRoot"]
        os.makedirs(versions_root, exist_ok=True)
        version = next_version(versions_root, ctx["index"])
        base_version = latest_version
        mode = args.mode or ("full" if not base_version else "incremental")
        if mode not in MODES:
            fail("invalid audit mode: " + str(mode))
        if not base_version:
            mode = "full"
        architecture_change = ctx["architectureDelta"]
        affected = ctx["affectedModules"]
        scope = version_scope(args, state, mode, affected, architecture_change)
        issue_changes = issue_delta(previous_state, state)
        created_at = now_iso()
        try:
            semantic_receipt = contract.build_preflight_receipt(
                state,
                ctx["currentSnapshot"]["sourceFingerprint"],
                flatten_values(args.gate),
                scope,
                base_version,
                mode,
                project_type,
                created_at,
            )
        except AuditContractError as exc:
            fail(format_contract_error(exc))
        published_state = json.loads(json.dumps(state, ensure_ascii=False))
        published_state["schemaVersion"] = SCHEMA_VERSION
        published_state["semanticValidation"] = semantic_manifest_summary(semantic_receipt)
        published_state["auditVersion"] = {
            "version": version,
            "mode": mode,
            "baseline": base_version,
            "trigger": args.trigger,
            "scope": scope,
            "createdAt": created_at,
            "sourceFingerprint": ctx["currentSnapshot"]["sourceFingerprint"],
        }
        published_state["auditDelta"] = issue_changes if base_version else None

        stage = tempfile.mkdtemp(prefix=".staging-{}-".format(version), dir=versions_root)
        target = os.path.join(versions_root, version)
        if os.path.exists(target):
            fail("refusing to overwrite existing version: " + version)

        try:
            write_json(os.path.join(stage, "modules.json"), published_state)
            copy_artifact(standard_path, os.path.join(stage, "standard.json"))
            staged_standard = read_json(os.path.join(stage, "standard.json"))
            if stable_json_hash(staged_standard) != contract.standard_fingerprint:
                fail("STANDARD_DRIFT detected while publishing; no version was promoted")
            write_json(os.path.join(stage, "semantic-receipt.json"), semantic_receipt)
            config = os.path.join(os.path.dirname(state_path), "config.json")
            if os.path.isfile(config):
                copy_artifact(config, os.path.join(stage, "config.json"))
            template_path = os.path.abspath(args.template or default_template_path())
            if not os.path.isfile(template_path):
                fail("render template missing: " + template_path)
            render_staged_audit(stage, template_path)

            write_json(os.path.join(stage, "source-snapshot.json"), ctx["currentSnapshot"])
            write_json(os.path.join(stage, "delta.json"), {
                "schemaVersion": SCHEMA_VERSION,
                "baseVersion": base_version,
                "source": ctx["sourceDelta"],
                "modules": ctx["moduleDelta"],
                "architecture": architecture_change,
                "issues": issue_changes,
                "affectedModules": affected,
                "scope": scope,
                "mappedChangedFiles": ctx["mappedChangedFiles"],
                "unmappedChangedFiles": ctx["unmappedChangedFiles"],
            })

            artifact_hashes = {}
            for name in sorted(os.listdir(stage)):
                path = os.path.join(stage, name)
                if os.path.isfile(path):
                    artifact_hashes[name] = sha256_file(path)

            base_manifest_sha = None
            if ctx["index"].get("versions"):
                base_manifest_sha = ctx["index"]["versions"][-1].get("manifestSha256")
            manifest = {
                "schemaVersion": SCHEMA_VERSION,
                "version": version,
                "baseVersion": base_version,
                "baseManifestSha256": base_manifest_sha,
                "mode": mode,
                "trigger": args.trigger,
                "scope": scope,
                "expectedBaseline": args.expected_baseline,
                "validationGates": flatten_values(args.gate),
                "semanticValidation": semantic_manifest_summary(semantic_receipt),
                "toolVersion": TOOL_VERSION,
                "createdAt": created_at,
                "projectRoot": root,
                "projectType": project_type,
                "sourceFingerprint": ctx["currentSnapshot"]["sourceFingerprint"],
                "sourceFileCount": ctx["currentSnapshot"]["fileCount"],
                "sourceBytes": ctx["currentSnapshot"]["totalBytes"],
                "scoreSummary": score_summary(state),
                "auditComplete": scan_report.get("needs_audit_count", 0) == 0 and not scan_report.get("empty"),
                "sourceDeltaCounts": ctx["sourceDelta"]["counts"],
                "changedModules": sorted(set(
                    ctx["moduleDelta"]["added"]
                    + ctx["moduleDelta"]["removed"]
                    + ctx["moduleDelta"]["contentChanged"]
                    + ctx["moduleDelta"]["auditChanged"]
                )),
                "affectedModules": affected,
                "architectureDelta": architecture_change,
                "issueDelta": issue_changes,
                "unmappedChangedFiles": ctx["unmappedChangedFiles"],
                "artifacts": artifact_hashes,
            }
            write_json(os.path.join(stage, "manifest.json"), manifest)

            # Re-read both product source and live audit artifacts. .codemap itself is
            # excluded, so only a real source mutation can change this fingerprint.
            stable_snapshot = collect_source_snapshot(
                root, ctx["currentSnapshot"], ctx["exactExcludes"])
            if stable_snapshot["sourceFingerprint"] != ctx["currentSnapshot"]["sourceFingerprint"]:
                fail("SOURCE_DRIFT detected while publishing; no version was promoted")
            os.replace(stage, target)
            stage = None
            manifest_sha = sha256_file(os.path.join(target, "manifest.json"))
            index = ctx["index"]
            entry = {
                "version": version,
                "createdAt": manifest["createdAt"],
                "mode": mode,
                "trigger": args.trigger,
                "sourceFingerprint": manifest["sourceFingerprint"],
                "manifestSha256": manifest_sha,
                "auditComplete": manifest["auditComplete"],
                "semanticValid": True,
                "contractFingerprint": semantic_receipt["contractFingerprint"],
            }
            index["schemaVersion"] = SCHEMA_VERSION
            index["latestVersion"] = version
            index["activeVersion"] = version
            index.setdefault("versions", []).append(entry)
            index.setdefault("rollbacks", [])
            update_live_artifacts_and_index(
                codemap,
                [(os.path.join(target, "modules.json"), state_path),
                 (os.path.join(target, "codemap.html"), html_path),
                 (os.path.join(target, "codemap.md"), md_path)],
                versions_root,
                index,
            )
            print(json.dumps({
                "status": "PUBLISHED",
                "version": version,
                "mode": mode,
                "path": target,
                "manifestSha256": manifest_sha,
                "semanticValidation": manifest["semanticValidation"],
                "scope": scope,
                "sourceDeltaCounts": manifest["sourceDeltaCounts"],
                "changedModules": manifest["changedModules"],
                "affectedModules": affected,
                "unmappedChangedFiles": manifest["unmappedChangedFiles"],
            }, ensure_ascii=False, indent=2))
        finally:
            # Failed staging is intentionally preserved for diagnosis. Successful
            # publication atomically moves it to the immutable target directory.
            pass


def verify_snapshot(snapshot):
    entries = snapshot.get("files") or []
    return snapshot.get("sourceFingerprint") == snapshot_fingerprint(entries)


def verify_retained_semantics(base, manifest, version):
    """Independently verify retained audit meaning, not only retained bytes."""
    errors = []
    compatibility = "legacy-contract"
    modules_path = os.path.join(base, "modules.json")
    standard_path = os.path.join(base, "standard.json")
    receipt_path = os.path.join(base, "semantic-receipt.json")
    if not os.path.isfile(modules_path):
        return {
            "version": version, "semanticValid": False,
            "compatibilityStatus": compatibility,
            "errors": ["[SEMANTIC_STATE_MISSING] missing retained modules.json"],
        }
    if not os.path.isfile(standard_path):
        return {
            "version": version, "semanticValid": False,
            "compatibilityStatus": compatibility,
            "errors": ["[SEMANTIC_STANDARD_MISSING] missing retained standard.json"],
        }
    try:
        state = read_json(modules_path)
        standard = read_json(standard_path)
        retained = AuditSchemaRegistry().read_retained(state)
        contract = AuditContract(standard)
        contract.validate_publishable_state(retained["document"])
        compatibility = retained["compatibilityStatus"]
    except (OSError, UnicodeError, ValueError) as exc:
        detail = format_contract_error(exc) if isinstance(exc, AuditContractError) else str(exc)
        return {
            "version": version, "semanticValid": False,
            "compatibilityStatus": compatibility,
            "errors": ["[SEMANTIC_STATE_INVALID] " + detail],
        }

    if os.path.isfile(receipt_path):
        compatibility = "current-contract"
        try:
            receipt = read_json(receipt_path)
            contract.validate_preflight_receipt(
                receipt,
                state,
                manifest.get("sourceFingerprint"),
                manifest.get("validationGates") or [],
                manifest.get("scope") or [],
                manifest.get("baseVersion"),
                manifest.get("mode"),
                manifest.get("projectType"),
            )
            summary = semantic_manifest_summary(receipt)
            if manifest.get("semanticValidation") != summary:
                errors.append(
                    "[RECEIPT_MANIFEST_MISMATCH] manifest semanticValidation does not match receipt")
            if state.get("semanticValidation") != summary:
                errors.append(
                    "[RECEIPT_STATE_MISMATCH] retained state semanticValidation does not match receipt")
        except (OSError, UnicodeError, ValueError) as exc:
            detail = format_contract_error(exc) if isinstance(exc, AuditContractError) else str(exc)
            errors.append("[SEMANTIC_RECEIPT_INVALID] " + detail)
    else:
        compatibility = "legacy-contract"
        if manifest.get("semanticValidation") is not None or state.get("semanticValidation") is not None:
            errors.append("[SEMANTIC_RECEIPT_MISSING] semantic metadata exists without a retained receipt")

    return {
        "version": version,
        "semanticValid": not errors,
        "compatibilityStatus": compatibility,
        "contractFingerprint": contract.contract_fingerprint,
        "stateFingerprint": contract.state_fingerprint(state),
        "errors": errors,
    }


def verify_repository(root, codemap, state_path, check_live=True):
    versions_root = os.path.join(codemap, "versions")
    index = load_index(versions_root)
    integrity_errors = []
    semantic_errors = []
    semantic_versions = []
    previous_manifest_sha = None
    listed = []

    for entry in index.get("versions", []):
        version = entry.get("version")
        listed.append(version)
        base = os.path.join(versions_root, str(version))
        manifest_path = os.path.join(base, "manifest.json")
        if not os.path.isfile(manifest_path):
            integrity_errors.append("missing manifest: " + str(version))
            continue
        manifest_sha = sha256_file(manifest_path)
        if manifest_sha != entry.get("manifestSha256"):
            integrity_errors.append("manifest hash mismatch: " + str(version))
        try:
            manifest = read_json(manifest_path)
        except (OSError, ValueError, TypeError) as exc:
            integrity_errors.append("cannot read manifest {}: {}".format(version, exc))
            continue
        if manifest.get("version") != version:
            integrity_errors.append("manifest version mismatch: " + str(version))
        if manifest.get("baseManifestSha256") != previous_manifest_sha:
            integrity_errors.append("hash chain mismatch: " + str(version))
        for name, expected in (manifest.get("artifacts") or {}).items():
            path = os.path.join(base, name)
            if not os.path.isfile(path):
                integrity_errors.append("missing artifact: {}/{}".format(version, name))
            elif sha256_file(path) != expected:
                integrity_errors.append("artifact hash mismatch: {}/{}".format(version, name))
        snapshot_path = os.path.join(base, "source-snapshot.json")
        if os.path.isfile(snapshot_path) and not verify_snapshot(read_json(snapshot_path)):
            integrity_errors.append("source snapshot fingerprint mismatch: " + str(version))

        semantic = verify_retained_semantics(base, manifest, version)
        semantic_versions.append(semantic)
        for message in semantic["errors"]:
            semantic_errors.append("semantic validation failed: {}: {}".format(version, message))
        if semantic["compatibilityStatus"] == "current-contract":
            if entry.get("semanticValid") is not True:
                semantic_errors.append(
                    "semantic validation failed: {}: index semanticValid is not true".format(version))
            if entry.get("contractFingerprint") != semantic.get("contractFingerprint"):
                semantic_errors.append(
                    "semantic validation failed: {}: index contract fingerprint mismatch".format(version))
        previous_manifest_sha = manifest_sha

    dirs = sorted(name for name in os.listdir(versions_root) if VERSION_RE.match(name)) \
        if os.path.isdir(versions_root) else []
    if dirs != listed:
        integrity_errors.append("version directories and index differ")
    expected_latest = listed[-1] if listed else None
    if index.get("latestVersion") != expected_latest:
        integrity_errors.append("latestVersion does not match index tail")

    active = index.get("activeVersion") or expected_latest
    if active and active not in listed:
        integrity_errors.append("activeVersion is not present in the version index")

    live_matches_active = None
    if check_live and active and os.path.isfile(state_path):
        try:
            live_state = read_json(state_path)
            live_html, live_md = output_paths(root, state_path, live_state)
            active_base = os.path.join(versions_root, active)
            active_manifest = read_json(os.path.join(active_base, "manifest.json"))
            pairs = ((state_path, "modules.json"), (live_html, "codemap.html"),
                     (live_md, "codemap.md"))
            mismatches = []
            for live_path, artifact in pairs:
                expected = (active_manifest.get("artifacts") or {}).get(artifact)
                if not expected or not os.path.isfile(live_path) or sha256_file(live_path) != expected:
                    mismatches.append(artifact)
            live_matches_active = not mismatches
            if mismatches:
                integrity_errors.append("working audit artifacts do not match activeVersion: "
                                        + ", ".join(mismatches))
        except (OSError, ValueError, TypeError) as exc:
            integrity_errors.append("cannot verify working audit artifacts: " + str(exc))

    current = None
    if os.path.isfile(state_path):
        try:
            ctx = current_context(root, codemap, state_path)
            delta = ctx["sourceDelta"]["counts"]
            current = sum(delta[k] for k in ("added", "removed", "modified")) == 0 \
                and ctx["scan"].get("up_to_date", False)
        except SystemExit as exc:
            integrity_errors.append(str(exc))

    compatibility_values = {
        item.get("compatibilityStatus") for item in semantic_versions
        if item.get("compatibilityStatus")
    }
    if semantic_errors:
        compatibility_status = "semantic-invalid"
    elif compatibility_values == {"current-contract"} or not semantic_versions:
        compatibility_status = "current-contract"
    elif compatibility_values == {"legacy-contract"}:
        compatibility_status = "legacy-contract"
    else:
        compatibility_status = "mixed-contracts"
    errors = integrity_errors + semantic_errors

    return {
        "valid": not errors,
        "integrityValid": not integrity_errors,
        "semanticValid": not semantic_errors,
        "compatibilityStatus": compatibility_status,
        "versions": semantic_versions,
        "versionCount": len(listed),
        "latestVersion": index.get("latestVersion"),
        "activeVersion": active,
        "workingAuditMatchesActive": live_matches_active,
        "currentSourceMatchesLatest": current,
        "errors": errors,
    }


def cmd_verify(args):
    root, codemap, state_path = codemap_paths(args.root, args.state)
    result = verify_repository(root, codemap, state_path, check_live=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["errors"]:
        raise SystemExit(1)


def cmd_rollback(args):
    root, codemap, state_path = codemap_paths(args.root, args.state)
    versions_root = os.path.join(codemap, "versions")
    reason = args.reason.strip()
    if not reason:
        fail("rollback requires a non-empty reason")
    with version_lock(codemap):
        index = load_index(versions_root)
        listed = [item.get("version") for item in index.get("versions", [])]
        if args.to not in listed:
            fail("rollback target is not a published version: " + args.to)
        history = verify_repository(root, codemap, state_path, check_live=False)
        if history["errors"]:
            fail("rollback target/history verification failed: " + "; ".join(history["errors"]))
        current_active = index.get("activeVersion") or index.get("latestVersion")
        if current_active == args.to:
            print(json.dumps({
                "status": "ALREADY_ACTIVE",
                "activeVersion": current_active,
                "latestVersion": index.get("latestVersion"),
            }, ensure_ascii=False, indent=2))
            return

        target = os.path.join(versions_root, args.to)
        manifest = read_json(os.path.join(target, "manifest.json"))
        if not manifest.get("auditComplete"):
            fail("rollback target is not a complete audit: " + args.to)
        target_state = read_json(os.path.join(target, "modules.json"))
        html_path, md_path = output_paths(root, state_path, target_state)
        replacements = [
            (os.path.join(target, "modules.json"), state_path),
            (os.path.join(target, "codemap.html"), html_path),
            (os.path.join(target, "codemap.md"), md_path),
        ]
        for optional in ("standard.json", "config.json"):
            source = os.path.join(target, optional)
            if os.path.isfile(source):
                replacements.append((source, os.path.join(codemap, optional)))
        record = {
            "fromVersion": current_active,
            "toVersion": args.to,
            "reason": reason,
            "createdAt": now_iso(),
        }
        index["activeVersion"] = args.to
        index.setdefault("rollbacks", []).append(record)
        update_live_artifacts_and_index(codemap, replacements, versions_root, index)
        print(json.dumps({
            "status": "ROLLED_BACK",
            "activeVersion": args.to,
            "latestVersion": index.get("latestVersion"),
            "reason": reason,
        }, ensure_ascii=False, indent=2))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="version architecture-review audits")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--root", required=True, help="project root")
        p.add_argument("--state", help="modules.json path (default: <root>/.codemap/modules.json)")

    status = sub.add_parser("status", help="read-only drift and next-audit status")
    common(status)
    status.set_defaults(func=cmd_status)

    publish = sub.add_parser("publish", help="publish an immutable architecture-audit version")
    common(publish)
    publish.add_argument("--mode", choices=sorted(MODES))
    publish.add_argument("--trigger", default="automatic_project_change")
    publish.add_argument("--scope", action="append", default=[],
                         help="audited module/dimension id; repeat or comma-separate")
    publish.add_argument("--expected-baseline",
                         help="fail closed unless latestVersion matches (use 'none' for v0001)")
    publish.add_argument("--gate", action="append", default=[],
                         help="validation gate evidence recorded and fingerprinted; any *:fail value rejects")
    publish.add_argument("--template",
                         help="render template (default: architecture-review/assets/template.html)")
    publish.add_argument("--force", action="store_true", help="publish even with no source/audit delta")
    publish.add_argument("--allow-incomplete", action="store_true",
                          help="relax scan completeness only; semantic audit validation is never bypassed")
    publish.add_argument("--allow-unknown", action="store_true",
                         help="explicitly audit a non-detected project type")
    publish.set_defaults(func=cmd_publish)

    verify = sub.add_parser(
        "verify",
        help="verify integrity, retained audit semantics, contract compatibility and current drift")
    common(verify)
    verify.set_defaults(func=cmd_verify)

    rollback = sub.add_parser("rollback",
                              help="restore the working audit view from a verified version without deleting history")
    common(rollback)
    rollback.add_argument("--to", required=True, help="published audit-vNNNN to activate")
    rollback.add_argument("--reason", required=True, help="human-readable rollback reason")
    rollback.set_defaults(func=cmd_rollback)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
