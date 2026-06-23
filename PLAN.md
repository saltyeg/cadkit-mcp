# cadkit — Roadmap

`cadkit` is a second MCP server (alongside `onshape_mcp`) for **idiomatic, fully-defined,
variable-driven** CAD authoring: one sketch carries entities + geometric constraints +
driving dimensions, grounded to the origin and parameterized by variables, with semantic
edge/face selection so downstream features reference topology by *meaning* rather than
transient IDs.

This roadmap is ordered so that **correctness and robustness of the parametric core come
before feature breadth** — a wide tool that emits under-defined or non-parametric geometry
would betray the thesis. Reorder freely; the tiers are a recommendation, not a contract.

## Current state (27 tools — P0–P2 shipped)
- Document/part-studio: `cad_document_create`, `cad_part_studio_create`
- Sketch session: `cad_sketch_begin` → `line`/`circle`/`rectangle`/`polyline` → `constrain`/`dimension` → `close`
- Variables: `cad_set_variable`, `cad_get_variables`
- Features: `cad_extrude`, `cad_fillet`, `cad_chamfer`, `cad_shell`, `cad_hole`, `cad_revolve`, `cad_mirror`, `cad_pattern`
- Inspection / lifecycle / I/O: `cad_measure`, `cad_delete_feature`, `cad_suppress`, `cad_edit_feature`, `cad_export`
- Semantic selection: `cad_find_edges` (circular/concave/convex/linear), `cad_find_faces` (planar-by-normal/cylindrical)
- Dev tooling: `cadkit_mcp/devkit.py` (quota-frugal verification helpers); on-demand live smokes in `scripts/`

Verified working: variable-driven dimensions drive the solid (a sketch drawn at the wrong
size snaps to its `#variable` values); semantic concave-edge → fillet; REMOVE-cut holes.

---

## P0 — Fix/harden the parametric core (the thesis depends on these)

1. **`cad_set_variable` must be idempotent (update-or-create).**
   Today each call appends a *new* `assignVariable` feature, so re-setting a variable makes
   a duplicate — and a duplicate placed after the sketch won't drive it. Look up an existing
   Variable feature by name (cache `get_features`) and update in place; create only if absent.
   *This is the single most user-visible gap (it confused the variable-editing workflow).*

2. **Fully-defined verification.**
   The "human pattern" claim hinges on 0-DOF sketches, but the builder can silently emit
   under-defined ones, which the solver then places unpredictably. Add:
   - `cad_sketch_close` returns a `degreesOfFreedom` / `fullyDefined` field.
   - optional `require_fully_defined=true` that fails loudly (and reports which entities are
     under-constrained) instead of shipping a fragile sketch.

3. **Parametric scalars everywhere.**
   `cad_extrude` depth, `cad_fillet`/`cad_chamfer` radius, pattern counts/spacing currently
   take bare floats. Accept a number **or** an expression/`#variable` (same `_expr` path the
   dimensions already use) so depth/thickness can be driven by variables too.

4. **Lightweight, quota-aware checks — NOT a broad live suite.**
   A full live test suite is counterproductive here: every assertion is a successful call
   against the 2,500/user/yr budget, so CI-on-push would drain it. Two proportionate layers:
   - **Offline builder tests (free, primary).** Assert on the JSON the builders *emit* —
     validate parameter ids/types against a cached `featurespec`, check the L-profile yields
     6 lines + ground + expected constraints. This catches the class of bug that actually hurt
     (the `assignVariable` wrong-`parameterId`) with **zero** API calls.
   - **One on-demand live smoke test (~6–8 calls), run manually before a release.** Only for
     truths offline can't prove: variable *drives* geometry, ground *pins*, concave→fillet.
     Via `ScratchStudio` + a single `measure_fs`. No CI, not on every change.

## P1 — Features needed for real parts

5. **`cad_hole`** ✅ *shipped (minimal)* — circles at given centers on a plane/face + a blind
   `REMOVE` extrude; diameter/depth accept `#variable`; multiple centers per call. Today a
   through-all hole = `depth` past the far face. **Next: counterbore / countersink** via the
   native Onshape `hole` feature. Its spec is huge/versioned (~150 params: V2/V3 duplicates,
   per-quantity tolerance bounds, lookup tables) — but only ~12 matter, recovered spec-first from
   the user's reference (`featureType:"hole"`, `holeVersion=V3`, `styleV2=HoleStyle.C_BORE` |
   `SIMPLE` | `C_SINK`, `holeDiameterV3`, `cBoreDiameterV3`/`cBoreDepthV3`,
   `cSinkDiameterV3`/`cSinkAngleV3`, `endStyleV2=BLIND/THROUGH`, `holeDepthV3`, `locations` +
   `scope` queries). Plan: a `cad_hole(style="simple|counterbore|countersink", …)` that emits the
   native feature with those params and leaves the tolerance/tap machinery at defaults. Tapped
   threads are a later add. Keep the simple circle-extrude path as a fallback.
6. **`cad_chamfer`** ✅ *shipped & verified* — equal-distance; `distance` accepts `#variable`.
   Two-distance / distance-angle still to add (the builder already carries the extra spec params).
7. **Sketch on a face / offset plane.** ✅ *shipped & verified* — `cad_sketch_begin(face=<id>)`
   targets a `cad_find_faces` result; offset planes still TODO.
8. **`cad_pattern`** (linear + circular) and **`cad_mirror`** ✅ *shipped (feature-based).*
   Reworked from the broken face form to **feature-based**: `patternType=FEATURE` + an
   `instanceFunction` (`BTMParameterFeatureList-1749`) holding the `featureIds` to repeat — the
   parameter the face form lacked. Ground truth was read back from hand-built, regenerating UI
   features (one `get_features` read, zero guessing): `MirrorType.FEATURE`/`PatternType.FEATURE`,
   geometry refs `mirrorPlane` / `directionOne` / `axis`, `operationType=NEW`. Builders emit the
   exact verified JSON; 3 offline regression tests pin the structure (and that the old `faces`
   form can't reappear). `cad_mirror(featureIds, planeId)`, `cad_pattern(kind, featureIds, …)`.
   *Optional follow-up:* an on-demand live regen smoke (~5 calls) — not run, since the emitted
   JSON is identical to features known to regenerate.
9. **`cad_revolve`** ✅ *shipped & verified* (region + axis edge; `angle` or full 360) and
   **`cad_shell`** ✅ *shipped & verified* (remove faces + inward `thickness`).

## P2 — Inspection, lifecycle, I/O  *(shipped & live-verified — `scripts/smoke_p2.py`)*

10. **`cad_measure`** ✅ *shipped* — built on `devkit.measure_fs`: solid count, total volume,
    and combined bounding box (min/max/size, inches) in a **single** eval. *Deferred:* mass /
    center of mass (need the `/massproperties` REST endpoint — no material density is set today)
    and point/edge/face distance (needs deterministic-id → query plumbing).
11. **Feature lifecycle** — `cad_delete_feature` ✅, `cad_suppress` ✅ (flip `suppressed`,
    update in place), `cad_edit_feature` ✅ (retarget one stored parameter — proven by editing an
    extrude depth and watching the measured volume double). **`cad_rollback` deferred** — the
    rollback bar is set via a distinct endpoint (`rollbackBarIndex`) not yet wrapped; lower value
    than the rest, do it spec-first later.
12. **`cad_export`** ✅ *shipped* — wraps the v11 translation endpoint (STL/STEP/PARASOLID/GLTF/
    OBJ; optional `partId`). Returns the async translation request (state `ACTIVE`); polling the
    translation to completion/download is a follow-up.
13. **`cad_get_variables`** ✅ *shipped* — lists name + **authored expression** by scanning the
    `assignVariable` features (one API call, unit-faithful — avoids the metre/inch ambiguity of
    resolving `getVariable`, and the `/variables` REST endpoint 404s on this tier anyway).

## Findings from the full-part integration test (`scripts/build_example_bracket.py`)

The angle-bracket build composes ~14 tools and self-checks by asserting measured geometry
against the variables (incl. editing `#leg` and confirming the solid grows). It surfaced two
bugs the per-tool smokes could not:

- **FIXED — teardrop/chamfered bore.** A circle is two semicircle arcs sharing a center but not
  a radius; a single diameter/radius dimension bound only the `.a` arc, leaving `.b` at the
  placeholder → a lopsided bore (and an oversized loose arc could split a thin wall, giving 2
  solids). `add_circle` now adds an `EQUAL` between the two arcs so one dimension drives the whole
  circle. Affected `cad_hole` *and* `cad_sketch_circle`; offline regression added.
- **OPEN — `cad_pattern`/`cad_mirror` of a subtractive feature errors.** Feature-pattern of an
  additive boss works (verified), but patterning a `REMOVE` hole errors and leaks a stray body
  (our `operationType=NEW`). Idiomatic workaround in place: repeat holes via multiple centers in
  one `cad_hole`. Real fix needs the correct op/scope for patterning a cut — discover spec-first
  from a hand-built "pattern of a hole" reference (as we did for the additive case).

## P3 — Selection & ergonomics

14. **Richer semantic selection** — largest/smallest face by area, faces/edges by position
    (highest Z, on a given plane), by adjacency, by tag. Reduce reliance on raw normals.
15. **Sketch ergonomics** — slots, arcs/fillets *within* a sketch, construction geometry,
    in-sketch mirror/pattern, auto-dimension-to-fully-defined helper.

---

## Cross-cutting principles (learned this session; apply to every new feature)

- **Spec-first, validate locally.** Before emitting a new feature type, fetch its published
  `featurespec` once and validate parameter ids/types locally. Onshape's value parameters are
  *type-specific and partly hidden* (the `assignVariable` bug: the visible-looking `value` field
  is `AlwaysHidden`; the real one is `anyValue`/`lengthValue`/…). Guessing wastes time; the spec
  is authoritative. Prototype unfamiliar JSON in the **browser FeatureScript console** (zero
  API-key cost).
- **Variables: `variableType=ANY` + `anyValue`** accepts any expression and is the general path.
- **Selection over transient IDs.** Keep emitting deterministic ids via read-only FeatureScript
  so features survive topology changes.
- **Measure `qBodyType(...,SOLID)`, not `qEverything(BODY)`** (default planes pollute bboxes);
  on the Front plane sketch-Y → world-Z. Both caused false "broken geometry" conclusions before;
  `devkit` encodes the correct forms.
- **Quota discipline.** 2,500 *successful* (`2xx`/`3xx`) calls per user per year; `429` is a
  burst limit (pace), `402` is annual exhaustion. Reuse one studio, batch within a feature,
  one eval per check, cache static reads.

## Definition of done for a feature
1. featurespec fetched + parameters validated locally · 2. emits fully-defined / parametric
output where applicable · 3. an **offline** builder assertion (validate emitted JSON; no API)
— add to the on-demand live smoke test only if behavior can't be proven offline · 4. example in
`examples/` and a line in the README · 5. PR targets `main`.
