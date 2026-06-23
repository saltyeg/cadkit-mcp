# cadkit — Roadmap

`cadkit` is a second MCP server (alongside `onshape_mcp`) for **idiomatic, fully-defined,
variable-driven** CAD authoring: one sketch carries entities + geometric constraints +
driving dimensions, grounded to the origin and parameterized by variables, with semantic
edge/face selection so downstream features reference topology by *meaning* rather than
transient IDs.

This roadmap is ordered so that **correctness and robustness of the parametric core come
before feature breadth** — a wide tool that emits under-defined or non-parametric geometry
would betray the thesis. Reorder freely; the tiers are a recommendation, not a contract.

## Current state (15 tools)
- Document/part-studio: `cad_document_create`, `cad_part_studio_create`
- Sketch session: `cad_sketch_begin` → `line`/`circle`/`rectangle`/`polyline` → `constrain`/`dimension` → `close`
- Variables: `cad_set_variable`
- Features: `cad_extrude`, `cad_fillet`
- Semantic selection: `cad_find_edges` (circular/concave/convex/linear), `cad_find_faces` (planar-by-normal/cylindrical)
- Dev tooling: `cadkit_mcp/devkit.py` (quota-frugal verification helpers)

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

5. **Proper `cad_hole`** (simple/counterbore/countersink/tapped, through-all or blind) instead
   of hand-rolled REMOVE extrudes — positioned by sketch points, sized by `#variable`.
6. **`cad_chamfer`** (equal-distance / two-distance / distance-angle), mirroring `cad_fillet`.
7. **Sketch on a face / offset plane.** Today sketches are limited to Front/Top/Right, which
   forced awkward hole placement. Add `cad_sketch_begin(face=<selected face>)` and offset
   planes, using `cad_find_faces` output as the target.
8. **`cad_pattern`** (linear + circular) and **`cad_mirror`** — drive counts/spacing by variables;
   select seed features/faces semantically.
9. **`cad_revolve`** and **`cad_shell`** — round out the common solid-modeling verbs.

## P2 — Inspection, lifecycle, I/O

10. **`cad_measure`** built on `devkit.measure_fs` — bbox, volume, mass, center of mass,
    point/edge/face distances — in a *single* eval.
11. **Feature lifecycle** — `cad_delete_feature`, `cad_suppress`, `cad_rollback`, and
    `cad_edit_feature` (change a stored parameter, e.g. retarget a dimension to a `#variable`).
12. **`cad_export`** (STL/STEP) for the part studio.
13. **`cad_get_variables`** / list — read the variable table back (FeatureScript `getVariable`,
    since the `/variables` REST endpoint 404s on this tier).

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
