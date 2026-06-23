# Example — variable-driven angle bracket

A worked, end-to-end cadkit part that doubles as the **full-part integration test**
(`scripts/build_example_bracket.py`). Run it against a scratch element:

```bash
venv/bin/python scripts/build_example_bracket.py
```

## What it builds

An L-section angle bracket, fully parameterized by four variables — `leg`, `width`,
`thick`, `hole_dia` — with two through-holes up the vertical leg and a filleted inner
corner.

1. **Variables** — the part's public parameters (`cad_set_variable`).
2. **Grounded, dimensioned L profile** on the Front plane (`cad_sketch_polyline` →
   `cad_sketch_dimension` to `#leg`/`#thick`), origin-anchored → fully defined.
3. **Body** — `cad_extrude` depth `#width`.
4. **Inner fillet** — `cad_find_edges(concave)` → `cad_fillet` radius `#thick * 0.8`
   (semantic selection: the inner corner is found by geometry, not a guessed id).
5. **Two holes** — one `cad_hole` with two centers (the repetition lives in the
   sketch; see the pattern-of-cut note below).
6. **Verification** — `cad_measure` asserts the bounding box equals the variables, then
   edits `leg` 2 → 2.5 and confirms the solid actually grows. This is the decisive
   proof that the variables *drive* the geometry end-to-end.
7. **Export** — `cad_export` STEP.

## Why this is a test, not just a build

The per-tool smokes prove one tool in isolation; this proves they **compose**. It found
two real bugs the smokes couldn't (a teardrop-bore from un-tied circle arcs; a
pattern-of-cut error) — see PLAN.md "Findings".

## Notes

- **Repeated holes** belong in one `cad_hole` (multiple centers), not a feature-pattern
  of the cut. `cad_pattern` is feature-based and currently errors when the seed feature
  is subtractive (a hole) — patterning *additive* features works.
- **Counterbore / countersink / tapped** holes are the native Onshape `hole` feature,
  which cadkit's simple `cad_hole` (circle + REMOVE extrude) does not yet emit — see
  PLAN.md P1 #5.
