# Selective website/game audits and immutable versions

Read this reference when `architecture-review` is explicitly requested, selected for a
website/game milestone or high-impact boundary, or used to publish/check audit history.

## Trigger contract

`software-engineering` remains the only generic automatic engineering entrypoint. For a
website, game, or browser-game hybrid, it routes here only when at least one of these is
true:

1. The user explicitly requests architecture review evidence, a module map, coupling or
   quality scores, staleness status, or a versioned refresh.
2. The work has reached a user-recognized milestone, architecture acceptance point, or
   formal pre-release review.
3. A high-impact boundary changed and focused verification cannot safely establish the
   impact: public core/contracts, data ownership, persistence/migration, permissions,
   engine/framework major versions, or the build/release chain.

Ordinary prototype iterations, small features, localized bug fixes, pure discussion,
read-only explanation, and fully rolled-back tasks do not create a version merely
because project files changed. They use focused stack-specific verification instead. An
explicit architecture review still uses this skill even without product mutation; use
`--force` only when the user wants a new no-delta review version.

A Skill cannot continuously watch edits made between reviews. When the next review is
triggered, `version.py status` compares the current content snapshot with the latest
version and reports accumulated edits as drift, so they enter that audit. A true
real-time watcher is a separate automation and is not implied by this contract.

## Persistent layout

The mutable latest map remains unchanged:

```text
<project>/.codemap/
├─ config.json
├─ modules.json
├─ standard.json             # optional project override
├─ codemap.html
├─ codemap.md
└─ versions/
   ├─ index.json               # latestVersion + activeVersion + rollback records
   ├─ audit-v0001/
   │  ├─ manifest.json
   │  ├─ semantic-receipt.json  # contract/standard/state/source/gate/scope binding
   │  ├─ modules.json
   │  ├─ codemap.html
   │  ├─ codemap.md
   │  ├─ standard.json
   │  ├─ config.json         # when present
   │  ├─ source-snapshot.json
   │  └─ delta.json
   └─ audit-v0002/
```

`modules.json` is still the only mutable source of truth for the current map. A version
directory is an immutable historical capture. Never edit a published version to correct
it; publish a successor and explain the correction in the current report themes or task
summary.

## Retained audit lifecycle

### When a review is triggered

Run the read-only status command:

```bash
python scripts/version.py status --root <project>
```

- `NEEDS_FULL_AUDIT`: there is no `.codemap/modules.json`; perform the normal `init`
  workflow for this review.
- `NEEDS_INCREMENTAL_AUDIT`: code or architecture state differs from the last version;
  include the reported delta in the pending audit.
- `UP_TO_DATE`: use the latest version as the baseline.

Do not hold a long-lived filesystem lock while ordinary product work runs. Publishing
uses an exclusive short lock plus a final source-stability check.

### First completed audit

Perform the existing full `init` workflow: decompose, scan, independently audit every
module, synthesize themes, render and stamp the baseline. Structure-only mode does not
satisfy a retained first audit.

Then publish:

```bash
python scripts/version.py publish --root <project> --mode full \
  --trigger milestone_or_explicit_review --expected-baseline none \
  --gate "focused-tests:pass"
```

Before allocating a version number or creating a promotable staging directory, publish
runs the shared `AuditContract` against the effective standard. This creates
`audit-v0001` only when every module is audited, no module path is empty, every semantic
invariant passes, and no supplied gate ends in `:fail`. `--allow-incomplete` may relax
workflow completeness only where explicitly supported; it never bypasses semantics.

### Later triggered reviews

Run the existing `update` workflow. The audit set is not only the text diff:

```text
directly changed modules
+ downstream/upstream consumers whose contracts are affected
+ new/deleted/renamed capabilities
+ relevant build/config/spec/resource changes
+ unresolved cross-cutting themes
```

Use the module content hash and `version.py status`; Git commit history is optional
context, never the only source of truth. After independent re-audits, scan, render and
baseline stamp, publish:

```bash
python scripts/version.py publish --root <project> --mode incremental \
  --trigger milestone_or_explicit_review --expected-baseline audit-v0001 \
  --scope changed_module --scope direct_consumer --gate "focused-tests:pass"
```

Use `expanded_incremental` when a small file delta changes a broad contract: public
API, persistence/schema, auth/permissions, game state ownership, scene loading,
framework/engine major version, build/release chain, or shared runtime assets. If the
impact cannot be bounded, run a full audit.

### No-delta behavior

If both the product source fingerprint and semantic architecture state match the latest
version, `publish` returns `NO_DELTA` and does not advance the version. Changes inside
`.codemap/**` and mutable HTML/MD outputs are excluded from the product fingerprint, so
the audit cannot recursively trigger itself.

## Source snapshot

The version tool records first-party project paths, size, mtime and SHA-256. It does not
require Git and reuses the previous hash when size+mtime are unchanged. It excludes:

- VCS, `.codemap`, CodeGraph, Codex temp/history;
- dependencies, engine caches, build/output/coverage directories;
- common caches, logs and temporary files;
- `.env*`, private keys and credential files (their contents are never read or stored).

HTML/MD outputs configured outside `.codemap` are also excluded as audit projections.
Game assets and formal project documents remain tracked unless they live in an excluded
build/output directory.

`status` reports directly changed modules plus direct/transitive consumers. The direct
consumers form the default suggested audit scope; a broad public contract uses
`expanded_incremental` and may include the transitive set. `delta.json` lists
added/removed/modified files, module content/audit changes, changed four-lens/eight-
dimension audit facts, issue-level new/resolved/persisting/unknown counts, the chosen
scope, file → module mappings and unmapped changed files.
Inspect every unmapped file: add a module if it represents a new capability; otherwise
record why it is configuration, documentation or non-module content. A dimension-only
evidence or verdict change is a semantic audit delta and may create a successor version
even when product source is unchanged.

## Immutability and concurrency

- Version numbers come from both the index and existing directories; never assign them
  by hand.
- Publishing stamps `auditVersion`/`auditDelta` into staged `modules.json`, regenerates
  HTML/Markdown from that exact state, rechecks product source, then atomically promotes
  the directory and recoverably updates the mutable working view plus index.
- Publishing records `schemaVersion` and `semanticValidation`, copies the exact effective
  standard, and writes `semantic-receipt.json` before promotion. The receipt fingerprints
  the contract, standard, canonical state, source, gates, scope, baseline and mode.
- `manifest.json` hashes every artifact. `versions/index.json` stores each manifest hash,
  and each successor manifest stores the prior manifest hash, forming a chain.
- A short-lived `.codemap/version.lock` prevents two publishers. Do not delete it until
  the owning process/task is understood.
- Source drift during publish stops promotion. Re-run after project writers are quiet.
- Historical cleanup, compaction or deletion requires an explicit user request.

## Verification

After publishing:

```bash
python scripts/version.py verify --root <project>
```

Verification checks two independent layers. `integrityValid` covers the index, version
directories, manifest chain, artifact hashes, source snapshots and mutable artifacts.
`semanticValid` re-reads each retained `modules.json` plus its exact `standard.json`, runs
the shared contract again, and checks the receipt/state/manifest/index binding.
`compatibilityStatus` is `current-contract`, `legacy-contract`, `mixed-contracts`, or
`semantic-invalid`. Overall `valid` is true only when both layers pass. Therefore a fully
rehashed but semantically impossible audit still fails verification.
Do not claim a retained audit version if this command fails.

Versions created before semantic receipts are not rewritten. A no-receipt v1 version is
read-only `legacy-contract`: verification independently checks its retained meaning and
hash chain, while future or unsupported schema versions fail closed.

## Structured rollback

Rollback selects an already published, verified audit as the working view. It never
changes product source, deletes a newer audit, or rewrites the chronological latest:

```bash
python scripts/version.py rollback --root <project> \
  --to audit-v0001 --reason "known-good audit view"
python scripts/version.py verify --root <project>
python scripts/version.py status --root <project>
```

`latestVersion` remains the index tail; `activeVersion` becomes the selected version.
The working `modules.json`/HTML/Markdown and retained standard/config are restored from
that version through a recoverable transaction, and the reason is appended to the index.
Rollback requires an explicit command and reason. Never delete history or an unknown
writer lock as a substitute for rollback.

## Website profile

For websites, expand the functional module map and audit to the changed parts of:

- pages/routes/navigation, deep links and permission routing;
- responsive UI, accessibility, loading/empty/error states;
- forms, validation, duplicate submission and focus behavior;
- API/data/auth boundaries and migrations;
- build, environment, asset path, deployment and rollback configuration.

## Game profile

For games, expand to the changed parts of:

- input/HUD/touch ownership and prompt consistency;
- scene loading, state owners, delayed callbacks and transitions;
- combat, death, reward and exact-once settlement boundaries;
- saves, migration, recovery and memory/disk consistency;
- runtime assets, manifests, audio/VFX ownership and candidate isolation;
- collision/visual coordinates, target platforms, loading and performance.

## Acceptance scenarios for this feature

1. An ordinary prototype change or localized fix creates no audit version by itself.
2. An explicit/milestone first review publishes `audit-v0001` as `full`; a later
   triggered review with accumulated source changes publishes `audit-v0002` as
   `incremental`.
3. Non-Git projects are detected through content hashes.
4. External changes appear in the next `status` output.
5. A no-delta publish returns `NO_DELTA`.
6. `.codemap` output changes do not create product drift.
7. Old version artifact hashes remain valid after a new version.
8. An incomplete/stale module set cannot be published as a retained review.
9. Concurrent publishing and source drift cannot promote a bad version.
10. `verify` closes the index, hash chain and current-source evidence.
11. Published HTML/Markdown contain the same stamped version/delta as retained JSON.
12. A verified rollback changes `activeVersion`, preserves `latestVersion`, and keeps all
    version directories and hashes valid.
13. Invalid score types/ranges, grade mismatches, `clean` conflicts, unknown tags,
    evidence-free negative tags, stale audit hashes and failed gates cannot be published,
    including with `--allow-incomplete`.
14. A forged retained state with recomputed artifact/manifest/index hashes reports
    `integrityValid=true` but `semanticValid=false`.
15. Current versions close a semantic receipt; old v1 no-receipt versions remain
    read-only compatible and are never rewritten during verification.
