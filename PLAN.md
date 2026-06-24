# cadkit — Roadmap

`cadkit` is an MCP server (alongside `onshape_mcp`) for **idiomatic, fully-defined,
variable-driven** CAD authoring in Onshape: one sketch carries entities + geometric
constraints + driving dimensions, grounded to the origin and parameterized by variables, with
semantic edge/face selection so downstream features reference topology by *meaning* rather than
transient IDs.

Ordering principle: **correctness and robustness of the parametric core before feature
breadth.** Tiers are a recommendation, not a contract.

---

## Current state — shipped & live-verified

The parametric core, the P1–P3 feature set, and OAuth are done. Summary of capability:

- **Authoring** — document/part-studio create; sketch session (`line`/`circle`/`arc`/`fillet`/
  `mirror`/`pattern`/`rectangle`/`polyline`/`slot` → `constrain`/`dimension` → `analyze`
  (offline DOF report / auto-dimension) → `close`); variables (`cad_set_variable` idempotent
  update-or-create, `cad_get_variables`).
- **Features** — `cad_extrude`, `cad_fillet`, `cad_chamfer`, `cad_shell`, `cad_hole`
  (simple/counterbore/countersink), `cad_revolve`, `cad_plane` (offset datum), `cad_mirror`,
  `cad_pattern` (linear + circular, feature-based, `operation=NEW/REMOVE`).
- **Inspection / lifecycle / I/O** — `cad_measure` (solid count, volume, bbox in one eval),
  `cad_delete_feature`, `cad_suppress`, `cad_edit_feature`, `cad_export` (translation request).
- **Semantic selection** — `cad_find_edges` / `cad_find_faces` by meaning (circular/concave/
  convex/linear/extreme/on-plane; planar-by-normal/cylindrical/largest/smallest/adjacent).
- **Auth** — API-key Basic (default) **and** OAuth2 authorization-code (`cadkit_mcp/oauth.py`,
  `cadkit-auth` CLI). Pluggable async auth provider on `OnshapeClient`; server prefers a stored
  OAuth token with silent refresh + rotation. **Live-verified** end-to-end (token round-trip via
  `/api/users/sessioninfo`, all three scopes granted).
- **Quality** — 567 offline tests; quota instrumentation (`cad_api_calls`); on-demand live
  smokes in `scripts/`; two full integration builds (`build_example_bracket.py`,
  `build_example_flange.py`).

Integration tests proved what per-tool smokes can't: variables genuinely *drive* the solid,
ground *pins* it, concave→fillet selection holds, REMOVE-patterns cut (1 solid, not stray
bodies), and a cylindrical face works as a circular-pattern axis for concentric parts.

---

## P4 — Parametric-core gaps (finish the thesis)

1. **Parametric sketch pattern construct** — ✅ *shipped for circles, live-verified.*
   `cad_sketch_pattern` on a single **circle** now emits the real `LINEAR_PATTERN` /
   `CIRCULAR_PATTERN` sketch constraint (`add_linked_circle_pattern`), not loose geometric copies —
   so it's one editable pattern, not N independent circles. Lines / multi-entity still fall back to
   geometric copies (their localInstance role schema isn't read back yet); arcs TODO.
   Ground-truth read-back from a UI build was the method (zero guessing). Findings:
   - **Unblocked by the single-curve circle refactor** — the construct operates on whole-circle
     entities, which 2-arc circles couldn't supply.
   - **Per-type role index is reversed** (verified, not a misread): linear enumerates
     `localInstance0,k,0`=curve / `1,k,0`=center; circular `localInstance1,k`=curve / `0,k`=center.
   - **`patterng`/`maximumpatterng` are manipulator state, not the count** — emit the constant `2`
     (what the UI emits for both types); the real count is `patternc1` + the localInstance list.
     Using `count-1` regenerates WARNING when `count-1 != 2` (caught live: circular count 4).
   Live-verified (`scripts/smoke_sketch_pattern_linked.py`): circular ×4 + linear ×3 both close OK
   (not WARNING) and extrude to 4+3=7 solids. Still TODO: count/spacing as `#variables`, lines/arcs.
2. **Hole/pattern centers as variables.** Seed bolt sits at a literal BCD coordinate and counts
   are literals (flange finding) — editing a bolt-circle variable won't move them. Thread
   `#variable`/expressions into center placement and pattern counts.
3. **Auto-dimension-to-fully-defined helper** — ✅ *shipped (offline); no oracle exists.*
   `cad_sketch_analyze(apply=false)` reports `{dof, grounded, fullyDefined, hints, applied}` with
   **zero API calls** — it reasons over the session's in-memory entities + constraints. `dof` is an
   analytic estimate (entity DOF − nominal constraint removal, floored): exact for tree-like
   constraint graphs, approximate under coupling/redundancy, so it drives the `fullyDefined`
   verdict while per-entity `hints` stay advisory. `apply=true` auto-dimensions only the **safe,
   unambiguous** cases by locking the *current* sketched geometry (diameter on un-sized
   circles/arcs; H/V on axis-aligned lines) — it never adds line lengths (a closed loop couples
   them) and never grounds (anchor placement is a design choice), so it can't over-constrain.
   Dimensions-only by design (no guessed geometric constraints). 6 offline builder tests.
   **Oracle-verify resolved (researched, not probed):** Onshape exposes **no** sketch DOF /
   fully-constrained status — *not* via FeatureScript (FS-evaluated sketch entities are fixed; the
   solver state isn't readable) and *not* via REST (Onshape forum: "no way currently to retrieve
   the constraint status of a sketch through the API"). The only API signal is `featureStatus=
   WARNING` on an **over**-constrained sketch — which `cad_sketch_close` already returns. So 0-DOF
   stays analytically asserted; there is nothing to wire a `verify=true` to. Item complete.
4. **Offset construction planes** — ✅ *shipped & live-verified.* `cad_plane(reference, offset,
   flip)` emits the `cPlane` OFFSET datum (params `cplaneType=CPlaneType OFFSET` / `offset` —
   regenerates OK), resolves the new plane's deterministic id, and returns `planeId` to feed
   `cad_sketch_begin(face=planeId)`. **Gotcha (live-found):** a sketch targets the plane's planar
   **FACE**, not its body — `qCreatedBy(makeId(fid), EntityType.FACE)` (BODY id is rejected as a
   sketch plane). Verified: plane Top+3″ → 1×1 sketch on it → extrude → solid min-Z = 3.0 (the
   sketch genuinely sat on the lifted plane), all features OK (`scripts/smoke_offset_plane.py`).
   Still TODO: angle / point-defined / mid-plane datum types (this ships OFFSET only).

## P5 — Feature breadth (deferred, do spec-first)

5. **Hole follow-ups** — tapped threads (template already carries tap params), two-distance
   chamfer on holes, auto-pick `up` from body-vs-plane position.
6. **Chamfer variants** — two-distance / distance-angle (builder already carries the spec params).
7. **`cad_rollback`** — wrap the `rollbackBarIndex` endpoint (distinct from feature add).
8. **Export polling** — poll the async translation to completion + download (today returns the
   `ACTIVE` request only).
9. **Measure extensions** — mass / center of mass via `/massproperties` (needs a material density
   set); point/edge/face distance (needs deterministic-id → query plumbing).
10. **Selection by tag**; mirror/pattern of **arcs & circles** in-sketch (lines only today).

## P6 — Productization (the bring-your-own-agent path)

OAuth2 is the architectural prerequisite and is now in place. The remaining work is a *product*
decision, not a code task — track it here so it isn't lost. See the quota escape-hatches memory.

11. **Confirm the actual quota tier.** Live `sessioninfo` reports `planGroup: EDU Educator`, not
    the student tier the 2,500/yr assumption is based on — verify the real annual ceiling with
    Onshape before sizing anything.
12. **App Store publication** (only if cadkit becomes a public product). A *private* OAuth app's
    calls still count against the quota — the exemption requires publishing. Gated on the full
    Launch Checklist (developer agreement, ≥5 beta testers, Onshape QA, store listing, support
    SLA). Revisit only with explicit intent to ship publicly.
13. **Cheaper headroom alternatives** (if it stays a dev tool) — buy calls via
    `api-support@onshape.com`, or upgrade tier. Keep the offline-first discipline either way.

---

## Cross-cutting principles (apply to every new feature)

- **Spec-first, validate locally.** Fetch a feature type's published `featurespec` once and
  validate parameter ids/types locally before emitting. Onshape value parameters are
  type-specific and partly hidden (the `assignVariable` bug: visible `value` is `AlwaysHidden`;
  the real field is `anyValue`/`lengthValue`/…). Prototype unfamiliar JSON in the **browser
  FeatureScript console** (zero API-key cost), not against the live API.
- **Variables: `variableType=ANY` + `anyValue`** accepts any expression — the general path.
- **Selection over transient IDs.** Emit deterministic ids via read-only FeatureScript so
  features survive topology changes.
- **Measure `qBodyType(...,SOLID)`, not `qEverything(BODY)`** (default planes pollute bboxes); on
  the Front plane sketch-Y → world-Z. `devkit` encodes the correct forms.
- **Quota discipline.** 2,500 *successful* (`2xx`/`3xx`) calls/user/year; `429` = burst (pace),
  `402` = annual exhaustion. Reuse one studio, batch within a feature, one eval per check, cache
  static reads. **A feature POST returning 200 with `featureStatus=ERROR` is a 2xx and counts** —
  blind iterate-on-JSON loops are *not* free; read the error in the UI. Watch `cad_api_calls`
  during live work (one bad session spent ~594 in a day).

## Definition of done for a feature

1. featurespec fetched + parameters validated locally · 2. emits fully-defined / parametric
output where applicable · 3. an **offline** builder assertion (no API) — add to the on-demand
live smoke only if behavior can't be proven offline · 4. example in `examples/` + a README line ·
5. PR targets `main`.
