---
name: architecture-review
description: >-
  Review a codebase's architecture by generating and incrementally maintaining an
  interactive module map plus optional per-module code-quality audits (health scores,
  smell findings, lines-of-code, and a clickable dependency graph). Use when the user
  asks for 架构审查, an architecture/module map, coupling analysis, code-quality scores,
  staleness checks, incremental architecture refreshes, or fixes for a reviewed module;
  also use at an explicit website/game milestone or when a high-impact architecture
  boundary cannot be safely covered by focused verification. Do not invoke it merely
  because ordinary website or game files changed. Retained first audits are full and
  later retained audits are immutable incremental versions.
  Module quality scores require independent audits against the fixed rubric.
---

# 架构审查 Skill — Interactive Architecture Map & Code-Quality Audit

Builds and maintains three coupled artifacts for a project:

1. **`modules.json`** — the source of truth: every *functional* module (not file) with
   its paths, dependencies, coupling, LoC, content hash, score, grade, tags, findings.
2. **`codemap.html`** — a self-contained interactive map (layered modules,
   dependency highlighting, health coloring, audit-report view).
3. **`codemap.md`** — the written report (per-layer scores, per-module LoC
   table, worst offenders, cross-cutting themes).

The default website entry is the beginner-facing **分 / 连 / 变 / 保** view. It is a
progressive-disclosure projection of eight professional dimensions, not a second audit:

- **分** — module responsibilities + module boundaries.
- **连** — contracts/interfaces + dependency direction.
- **变** — data/logic separation + state/composition + long-term evolution/migration.
- **保** — tests, Git, CI, documentation, retained versions, backups and rollback.

Keep the landing page deliberately minimal: four text tabs, one key question, one
evidence-backed verdict, at most three priority issues, one version-delta line, and the
necessary drill-down links. Eight-dimension evidence, the existing module map/details,
the retained-version view, and the editable standard remain available only on demand.

The HTML and MD are **always regenerated** from `modules.json` by `render.py`. Never
hand-edit them. The state file makes everything **incremental**: a content hash per
module tells us exactly what changed and what needs re-auditing.

## Selective website/game audit contract

`software-engineering` remains the only generic automatic engineering entrypoint.
Ordinary prototype iterations, small features, and localized bug fixes stay outside
this skill and use the stack's focused parse/build, launch smoke, gameplay-path, and
affected-regression checks. An existing audit may remain stale until the next real
review trigger.

Enter this skill for a website, game, or browser-game hybrid only when the user asks for
architecture evidence, the work reaches an explicit milestone/release review, or a
high-impact boundary (public contract/core, data ownership, persistence/migration,
permissions, engine/framework major version, or build/release chain) cannot be safely
covered by focused verification. Once triggered:

1. Run `scripts/version.py status --root <project>` to identify the latest retained
   baseline and accumulated drift.
2. With no prior map/version, complete the normal full `init` audit.
3. With a prior version, complete `update` for the changed modules **and affected
   consumers**, not only the text diff.
4. Publish an immutable `audit-vNNNN` and run `version.py verify` before delivery.

Pure discussion, read-only explanation, ordinary project changes below the trigger
threshold, and a task with no final project delta do not create a version. Changes made
between reviews are detected on the next `status`; real-time filesystem watching is a
separate automation. Read
[reference/VERSIONING.md](reference/VERSIONING.md) for trigger, snapshot, concurrency,
website/game profiles and version acceptance rules.

## Standard

The scoring rubric, smell taxonomy, severity levels, and the required subagent prompt
are fixed in **`reference/STANDARDS.md`** — read it and follow it verbatim. The state
schema is in **`reference/DATA_MODEL.md`**. Do not improvise scoring or invent tags.

**The standard is configurable per project.** A machine-readable copy lives in
`reference/standard.json` (rubric, severities, coupling, and the tag list with
descriptions). A project may override it by placing its own `standard.json` next to the
state file (`<project>/.codemap/standard.json`) — `render.py` picks the project
file first, else the skill default, and injects it into the map's editable **Standard**
page. **Honor the project's tag set:** when `.codemap/standard.json` exists, audit
modules using *its* tags (including any custom tags the user added) — that is how users
capture their own definition of a problem. Keep `STANDARDS.md` (the prose + subagent
prompt) and `standard.json` (the machine copy) in sync if you change the defaults.

## Conventions

- `SKILL_DIR` = this skill's directory. Scripts are at `SKILL_DIR/scripts/*.py`,
  template at `SKILL_DIR/assets/template.html`. Use python3, stdlib only.
- **Everything lives under `<project>/.codemap/`** — one folder, not `.claude/`:
  - `config.json` — the user's saved preferences (UI language, output location, title…).
  - `modules.json` — the state (source of truth).
  - `standard.json` — optional per-project custom audit standard.
  - `codemap.html` + `codemap.md` — the generated outputs (default).
  - `versions/audit-vNNNN/` — immutable full/incremental audit snapshots plus a
    hash-chained `versions/index.json`. Never edit or overwrite a published version.

  The output location is a user preference: if they want the HTML/MD committed/visible,
  let them point it at `docs/` instead (ask — see `init` step 0). Set
  `meta.htmlPath` / `meta.mdPath` to wherever the outputs land so the reciprocal links
  are correct (both outputs sit in the same dir, so the in-page link uses the basename).
- A re-render command (run after any state change):
  ```
  python3 SKILL_DIR/scripts/render.py --state <state> \
    --template SKILL_DIR/assets/template.html \
    --out-html <htmlPath> --out-md <mdPath>
  ```

## Targeting modules without reading the whole state (`query.py`)

`modules.json` can be large. To decide what to audit/fix/test, DO NOT read the whole
file — use `scripts/query.py` to select exactly the modules you need and get back just
ids, file globs, or findings. This keeps agent context small.

```
# ids of every C-and-below module (feed a fix/audit loop)
python3 SKILL_DIR/scripts/query.py --state <state> --max-grade C --format ids
# modules carrying a specific problem (compact table)
python3 SKILL_DIR/scripts/query.py --state <state> --tag dual-format
# only the file globs to read for the D/F modules → read just those files
python3 SKILL_DIR/scripts/query.py --state <state> --max-grade D --format paths
# the exact findings to fix for one tag, as text
python3 SKILL_DIR/scripts/query.py --state <state> --tag glue --format findings
# what needs re-auditing
python3 SKILL_DIR/scripts/query.py --state <state> --needs-audit --format ids
```

Filters (AND-combined): `--max-grade {A..F}` (that grade and worse), `--min-score/--max-score`,
`--tag T` (repeatable; ANY, or `--match-all`), `--sev HIGH|MED|LOW`, `--band`, `--coupling`,
`--needs-audit`. Output `--format`: `ids | paths | findings | table | json | count`. Use
`--format paths` to read ONLY the relevant source, and `--format ids` to drive the
per-module subagent loop — never load the full `modules.json` just to pick targets.

## Hard rules

1. **Every module score comes from an independent sub-task.** One sub-task audits one
   module against its `paths`, using the prompt in `reference/STANDARDS.md`. Never score
   inline in the main thread; never copy one module's score to another. Run them in
   parallel where the platform supports it (see *Capabilities & platform mapping*).
2. **Scripts are deterministic; only decomposition, auditing, and theme-synthesis are
   model work.** `scan.py` / `render.py` / `apply_audit.py` never make quality judgments.
3. **Hand-edit only the structure/decomposition fields in `modules.json`.** Audit facts
   (`score`, `grade`, `tags`, `findings`, `auditedHash/At/Rev`) must enter through
   `apply_audit.py`; scan-owned fields must enter through `scan.py`. Both use the
   validated atomic state repository. HTML/MD are generated. Run `scan.py --write`
   before every render so LoC/hashes are fresh.
4. **Functional modules, not files.** A module is a capability (a store, a handler
   group, a feature folder, a plugin). Map each to a glob set in `paths`. Give every
   module a 1-line `desc` ("what it does", shown on click) authored in `meta.lang`
   (set `meta.lang` to `"zh"`/`"en"`; it localizes the UI chrome — module names/ids are
   never translated).
5. **Four separate, independent subagent roles — never merge two:**
   **auditor** (scores quality), **test-author** (writes tests), **fixer** (changes
   code), **acceptance/verifier** (proves no regression). A fix is accepted ONLY when an
   independent acceptance subagent shows the pre-fix green tests are still green and the
   build/typecheck is clean. A fixer may not write/edit its own tests or grade its own
   work — that defeats the gate.
6. **Triggered website/game audits are retained.** The first retained audit must be
   complete (not structure-only); each later triggered review incorporates accumulated
   project drift and publishes an incremental or expanded successor when there is a real
   delta. Ordinary project changes do not trigger a review by themselves. `.codemap/**`
   never triggers itself. A no-delta review returns `NO_DELTA` and does not consume a
   version number.
7. **Four lenses never hide unknowns.** `architectureDimensions` contains exactly the
   eight canonical ids and is the professional fact layer. `architectureLenses` is only
   its four-way summary. Warning/risk requires current evidence; unsupported dimensions
   are `unknown` with `score: null`. Do not calculate dimension scores from finding counts.
8. **Hashes do not prove audit meaning.** `apply_audit.py`, `version.py publish`, and
   `version.py verify` must use the same `AuditContract`. New retained versions include
   `semantic-receipt.json`; delivery requires both `integrityValid` and `semanticValid`.
   A missing receipt is accepted only for independently validated read-only v1 history.

## Capabilities & platform mapping

This workflow needs three capabilities. Each has a graceful fallback, so it runs on any
agent — only the convenience changes, never the rules above.

| Capability | Native (Claude Code) | Codex / Cursor | Fallback if unavailable |
|---|---|---|---|
| **Independent sub-tasks** (one auditor/fixer per module) | `Agent` tool, many in parallel | their subagent/task tool | Audit modules **one at a time in the main thread** — still one module per pass against the rubric, never batch-scoring. Slower, fully valid. |
| **Structured result** (the audit JSON) | `schema` on the Agent call | tool-specific schema, or just ask for JSON | Ask the sub-task to return **only** the JSON object; `apply_audit.py` validates it and rejects malformed/inconsistent results — no schema feature required. |
| **Ask the user** (preferences on `init`) | `AskUserQuestion` | tool's prompt UI | Ask in plain text, or apply defaults (`lang=en`, output `.codemap/`, title = repo folder name) and tell the user how to change them in `.codemap/config.json`. |

The non-negotiables (independent per-module audit, deterministic scripts, the four-role
fix gate) hold on every platform; the table only changes *how* you spawn the work.

## Token efficiency

Almost all the cost is the per-module audit sub-tasks reading code — the scripts are
nearly free. Levers, biggest first:

1. **Audit on the cheapest capable model.** The audit is read-code + apply-fixed-rubric +
   emit-JSON; a small/fast model does it well, and `apply_audit.py` rejects bad output.
   Keep the top model for decomposition, theme synthesis, and fixes only.
2. **Read targeted, not whole.** The audit prompt (STANDARDS.md) greps markers and reads
   only the flagged regions; a huge file is scored from its size + a few excerpts, not a
   full read. Use `query.py --format paths` so a sub-task opens only its module's files.
3. **Batch the small modules.** Group tiny / low-coupling leaves (≤ ~150 LoC) into one
   sub-task that audits each independently (see STANDARDS.md). Core / large / high-coupling
   modules stay solo. This cuts the *number* of spawns (each spawn re-pays system-prompt +
   rubric overhead). `query.py --max-score 100 --format json` then group by loc/coupling.
4. **`update`, not `init`.** After the first build, only ever run `update` — it re-audits
   just the git-changed modules (`needs_audit`), so steady-state cost is tiny.
5. **Structure-first for big repos (explicit exploratory runs only).** Run `init` in
   **structure-only** mode (decompose + `scan --write` + render, *no audits*) to get the
   map and LoC instantly and cheaply; the HTML renders unscored modules fine. Then fill
   scores over time with `update` / on-demand audits, cheapest-first or
   worst-suspected-first. A retained website/game first audit cannot use this shortcut.

---

## Command: `init` (first build)

Use when no `modules.json` exists yet. (Also accepts `generate` as an alias.)

0. **Ask the user for preferences first** (use `AskUserQuestion` if available, else just ask
   in plain text; or apply the defaults from *Capabilities & platform mapping*), then save
   them to `<project>/.codemap/config.json`:
   - **UI language** — `en` or `zh` (localizes the map chrome + report; module names are
     never translated). → `meta.lang`.
   - **Output location** — where the HTML/MD go. Default `.codemap/` (kept with the tool
     data); offer `docs/` if they want them committed/visible. → `meta.htmlPath` / `meta.mdPath`.
   - **Project title** (defaults to the repo/folder name) and an optional one-line subtitle,
     in the chosen language. → `meta.project` / `meta.subtitle`.

   Write `config.json` like:
   ```json
   {"lang":"zh","project":"My App","subtitle":"…","outputDir":".codemap",
    "htmlFile":"codemap.html","mdFile":"codemap.md"}
   ```
   and apply it to `meta` when you build `modules.json`. Re-read `config.json` on later
   runs so preferences persist.
1. **Decompose the project into functional modules.** Explore the tree (parallel Explore
   agents for big repos). Identify capabilities and group them into **bands** (visual
   layers in data-flow order, e.g. UI → stores → transport → │wire│ → app → handlers →
   core → persistence → plugins). For each module record `id, label, band, path, paths
   (globs), coupling, deps, desc`. Add `bands`, `spine` (the critical request path), and
   `meta` (project, htmlPath, mdPath, spineDesc). Write this to `modules.json` (no scores
   yet). Coupling = structural centrality (low/med/high/core); core = the spine hubs.
2. **Compute size:** `python3 scripts/scan.py --root <proj> --state <state> --write`.
   It reports every module as `unaudited`.
3. **Audit — one independent sub-task per module, in parallel.** For each id in
   `needs_audit`, spawn a sub-task with the `reference/STANDARDS.md` prompt (filled with
   the module's label/paths). Collect each JSON result and apply it:
   `python3 scripts/apply_audit.py --state <state> --id <id> --json '<result>' [--rev <git rev>]`.
   Run on the cheapest capable model, read targeted excerpts, and batch the small/leaf
   modules per *Token efficiency* + STANDARDS.md. **Structure-only mode:** for a huge repo
   (or a fast/cheap first pass) you may SKIP this step entirely — render the map with no
   scores (it renders unscored modules fine), then fill scores later with `update`.
4. **Synthesize `reportThemes`** (4–7 cross-cutting patterns) from the collected findings
   and write them into `modules.json`.
5. **Render:** run the render command. Then **stamp the git baseline** so future updates
   can diff from here: `python3 scripts/scan.py --root <proj> --state <state> --stamp-rev`.
6. **Retain the audit:** when this is a triggered retained website/game review, run
   `python3 scripts/version.py publish --root <proj> --mode full --expected-baseline none`, followed by
   `python3 scripts/version.py verify --root <proj>`. Retained audits may not use
   `--allow-incomplete`. Report the audit version together with avg score, grade spread,
   worst offenders, and the two live artifact paths.

## Command: `check` (is the map current? — read-only)

Use when the user asks "is the architecture map up to date / still accurate?".

1. For a website/game, first run
   `python3 scripts/version.py status --root <proj>` to show the latest retained version,
   source-level delta (including non-Git/external edits), unmapped files and whether a
   full or incremental audit is due.
2. `python3 scripts/scan.py --root <proj> --state <state>` (no `--write`).
3. Read the JSON: report `up_to_date`, the **stale** list (code changed since audit),
   **unaudited** (new modules with no score), and **empty** (paths match nothing →
   likely deleted modules). The `git` block shows the **commits since the last codemap
   run** (`meta.rev`) and which modules they touched — surface those commits so the user
   sees recent history at a glance. Do **not** modify anything; offer to run `update`.
4. Also sanity-check for *new* capabilities not yet in `modules.json` (a quick look at
   new top-level dirs / large new files). New modules are model-discovered, not scan-detected.

## Command: `update` (incremental refresh, git-aware)

Use after code changes, or when `check` found drift. Re-audits only what changed, and
uses git to show recent history and scope the work.

1. **Reconcile structure first** (cheap): if modules were added/removed/renamed, edit
   `modules.json` (add new module entries with `paths`; drop `empty` ones; fix globs).
2. **Scan + git diff:** `python3 scripts/scan.py --root <proj> --state <state> --write`.
   Read the report's **`git`** block: `commits` (since `meta.rev`, the last run) and
   `changed_modules` (modules those commits touched). Show the user the recent commits —
   this is the fast "what changed" view. The audit set is `needs_audit` (= stale +
   unaudited); content-hash staleness already includes everything `changed_modules` lists
   (plus any uncommitted edits), so re-audit `needs_audit`. If `git` is null the project
   isn't a git repo — fall back to content-hash staleness only.
   For websites/games, also read `version.py status`: inspect added/removed/modified and
   unmapped files, reconcile new/deleted capabilities, and expand to consumers when a
   public contract changed even if their files did not.
3. **Re-audit only those modules**, each with its own independent subagent (same protocol
   as `init` step 3). Apply each via `apply_audit.py --id <id> --rev <head>`. Fresh
   modules keep their cached audit — that is the whole point of the content hash.
4. **Refresh `reportThemes`** if the changes are material (otherwise keep them).
5. **Refresh affected architecture dimensions.** Re-check only the dimensions touched by
   the delta; unchanged dimensions inherit the prior result with `inheritedFrom`. Then
   regenerate the four lens summaries. A public-contract change normally affects both
   `contract` and `dependency`; persistence changes normally affect `evolution` and
   `safeguards`; do not blindly mark every dimension stale.
6. **Render**, then **stamp the baseline**:
   `python3 scripts/scan.py --root <proj> --state <state> --stamp-rev` caches the current
   HEAD into `meta.rev`, so the next `update`/`check` diffs from here. Summarize which
   modules were re-scored and how their score moved, with the commits that caused it.
7. **Publish the successor:** for a website/game, run
   `python3 scripts/version.py publish --root <proj> --mode incremental --expected-baseline <latest>`, using
   `expanded_incremental` for broad public contracts. Then run `version.py verify`.
   `NO_DELTA` is a valid result and must not be converted into an empty version.

## Command: `version status|publish|verify|rollback`

Use these direct commands for version administration without changing the audit rules:

- `status` is read-only and reports latest/active versions, source/module delta, direct
  and transitive consumers, suggested audit scope, and unmapped changed files.
- `publish` is allowed only after `init` or `update` has closed every stale, unaudited and
  empty module and all eight dimensions/four lenses exist; it chooses the next version,
  first runs the shared semantic preflight, then emits a semantic receipt, stamps
  version/delta into staged state, regenerates both projections, and never overwrites an
  older one. `--allow-incomplete` is never a semantic bypass, and any `--gate *:fail`
  blocks promotion. Use `--scope`, `--gate`, and `--expected-baseline` to make the
  manifest answer what was audited, what passed, and which baseline was expected.
- `verify` independently re-reads every retained state and effective standard. It reports
  `integrityValid`, `semanticValid`, and `compatibilityStatus`, in addition to checking
  artifact hashes, source fingerprints, index entries, prior-manifest links and mutable
  artifacts against `activeVersion`. Current-contract history must have a matching
  receipt; v1 no-receipt history remains read-only `legacy-contract`. A verification
  failure blocks delivery and never repairs or rewrites history silently.
- `rollback --to audit-vNNNN --reason <text>` is the only supported audit-view rollback.
  It first verifies history, restores the selected version as the mutable working view,
  records the reason, keeps `latestVersion` and every historical directory, then requires
  a fresh `verify` and `status`. It never rolls back product code or assets.

## Command: `test <module>` (generate tests)

A **test-author subagent** generates tests for a module — independently of fixing. This
is also the prerequisite for a safe `fix` (it builds the regression net). Two modes:

- **characterization** (default before a fix): lock the module's CURRENT observable
  behavior so a later change can't silently alter it. Assert "same as today", not
  "correct".
- **coverage**: add missing unit tests for the module's public surface and the behaviors
  named in its `findings`.

Steps:
1. **Detect the repo's test framework + location** (pytest / jest / vitest / go test / …)
   from existing tests near the module; match their style and placement. Do NOT invent a
   new framework or harness.
2. **One test-author subagent** writes tests against the module's `paths`, runs them, and
   iterates until green on the CURRENT (unmodified) code. It reports: files added, what
   behavior is now locked, and a coverage note. If a test only passes by asserting a known
   bug, it must FLAG the bug, not bake it in as desired behavior.
3. **Tests are real source** — they stay in the tree (they are the regression net). Record
   their globs in the module's `tests` field in `modules.json`. Re-run `scan.py --write`
   and `render.py` (test LoC is tracked but excluded from the module's own audit scope).

Keep test-author distinct from fixer and auditor.

## Command: `fix <module-or-finding>` (auto-fix, regression-gated)

Use when the user says "fix the findings in module X" / "auto-fix the worst offenders".
A fix is **only accepted if an independent acceptance subagent proves no regression.**
Four separate subagents (hard rule 5): test-author → fixer → acceptance → auditor.

1. **Scope (via `query.py`).** Resolve the target set with `query.py` instead of reading
   the whole state — e.g. `--max-grade C --tag dual-format --format ids` for "all C-and-
   below dual-format modules", then `--format findings` for just the findings to fix and
   `--format paths` for just the files to read. Confirm with the user before risky fixes
   (duplication merges, dual-format removal touching a protocol, deleting "dead" code —
   first verify it is truly unused).
2. **Baseline (test-author subagent).** Ensure the module has tests that lock its CURRENT
   behavior; if coverage is thin, run `test <module>` (characterization mode) first. Run
   the module's tests + the narrowest build/typecheck on the UNMODIFIED code and record
   the **green baseline** (which tests pass, build/type status, key outputs). If you can't
   get a green baseline, STOP and tell the user — auto-fixing without a behavioral net is
   not safe.
3. **Fix (fixer subagent, `isolation: "worktree"`).** Give it the module's paths, findings,
   and `reference/STANDARDS.md` rules; implement the fix and preserve behavior. The fixer
   **must not edit tests** (no moving the goalposts) and must not touch files outside its
   paths without flagging.
4. **Acceptance gate (independent verifier subagent — NOT the fixer).** Re-run the SAME
   baseline tests + build/typecheck on the fixed code. Return
   `{pass: bool, regressions: [...], evidence: "..."}`. **PASS only if every
   baseline-green test is still green and there are no new build/type errors.** On FAIL:
   report the regression with evidence and revert / hand back to the fixer — do not accept.
5. **Re-audit (auditor subagent, independent).** Only after PASS: re-score the module →
   `scan.py --write` → `apply_audit.py` → render. Show **before/after score AND the
   acceptance evidence** (tests run, all green, build clean).
6. **Never auto-commit unless asked.** Report honestly — a fix that fails the gate is
   reported as failed, not merged. Record the outcome in the module's `lastFix` field.

---

## Notes

- **Coupling vs score are independent** (structural vs quality) — see DATA_MODEL.md. The
  map can color by either (toggle in the header).
- **Big repos:** parallelize decomposition (Explore) and auditing (one agent per module).
  Chunk audits if there are many dozens of modules.
- **Determinism and trust:** same `modules.json` → identical HTML/MD. Commit
  `modules.json` so the audit history and incremental diffs are reviewable, but never
  treat byte/hash identity as semantic acceptance; use `version.py verify`.
- **Languages:** the engine is language-agnostic — `paths` globs and LoC counting work
  for any stack; the subagent reads whatever code the globs point at.
