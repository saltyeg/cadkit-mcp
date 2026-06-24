# FS-console probe — does Onshape expose true sketch DOF?

`cad_sketch_analyze` reports an **analytic** DOF estimate (exact for tree-like constraint
graphs, approximate under coupling). The open question for the `verify` path (P4-3) is whether
FeatureScript can hand us the solver's *ground-truth* DOF / under-constrained entities. The
existing `SketchSession.diagnostics()` docstring asserts it can't — this probe settles that
claim at **zero API-key cost** (the browser FeatureScript console doesn't spend quota).

## How to run (≈2 min, no quota)

1. Open any part studio that has at least one **under-constrained** sketch (e.g. a lone circle
   with no dimensions). The existing test doc works:
   <https://cad.onshape.com/documents/550b17de256c3edec2066d48/w/53187ebf4f6319f9815dc853>
2. Top menu → **Tools → Show FeatureScript console** (or the `</>` icon).
3. Paste the snippet below, run it, and copy back whatever prints.

## Snippet

```fs
FeatureScript 2426;
import(path : "onshape/std/geometry.fs", version : "2426.0");

// Enumerate sketch features and probe for any solver/DOF state we can read.
export const probe = function(context is Context)
{
    for (var f in evaluateFeatureList(context))   // names + types of every feature
    {
        println("feature: " ~ f);
    }
    // Candidate ground-truth sources to try (uncomment one at a time; some may not resolve —
    // a "function does not exist" error is itself a useful answer, tells us it's not exposed):
    //   - sketch solve status on a feature's regen data
    //   - any qDegreesOfFreedom / under-constrained query
    // Report what, if anything, exposes a DOF count or an "isFullyConstrained" flag.
};
```

> `evaluateFeatureList` may not be the exact spelling in your std version — if it errors,
> just `println` whatever feature-introspection function autocompletes in the console. The
> goal is only to learn **whether any field/function returns a sketch DOF count or a
> per-entity fully-constrained flag.**

## What to report back

- **If yes** (some call returns DOF or a fully-constrained flag): paste the call + output.
  → We wire `cad_sketch_analyze(verify=true)` to read it via one read-only FS eval (1 API call),
    giving an *exact* verdict and a cross-check on the analytic estimate.
- **If no** (nothing exposes it — confirming the docstring): say so.
  → `verify` instead just re-reads geometry to confirm the applied dims didn't throw a WARNING,
    and we document that 0-DOF is asserted analytically, not by oracle.

Either outcome is fine — this only decides how `verify` is implemented, not whether the
offline analyzer ships (it already has).
