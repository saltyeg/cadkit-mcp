"""Full-part integration test / worked example: a variable-driven angle bracket.

Unlike the per-tool smokes, this proves the tools COMPOSE — a whole part built the
cadkit way (grounded, dimension-driven, semantic selection) — and self-checks by
asserting the MEASURED geometry against the variables. The decisive check is at the
end: edit `leg` and confirm the bounding box actually moves (variable truly drives
the solid, end-to-end through the MCP dispatch).

Doubles as examples/ documentation. Budget: ~18 successful API calls.

The L cross-section (Front plane: sketch-X -> world-X, sketch-Y -> world-Z; the
extrude runs along the plane normal, -Y), origin at the inner corner:

    (0,leg) ___ (thick,leg)
       |   |
       |   |  vertical leg
       |   |___________ (leg,thick)
       |    ___________|
       |___|           (leg,0)
    (0,0)  (thick,0)        base leg
"""
import asyncio
import json

from cadkit_mcp import server as S

DOC = "550b17de256c3edec2066d48"
WS = "53187ebf4f6319f9815dc853"

LEG, WIDTH, THICK, HOLE_DIA = 2.0, 1.5, 0.25, 0.15


async def call(tool, **a):
    a = {"documentId": DOC, "workspaceId": WS, **a}
    r = await S.dispatch(tool, a)
    text = r[0].text
    if text.startswith("ERROR"):
        raise RuntimeError(text)
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        return text  # some tools (constrain/dimension) just return "ok"
    if isinstance(out, dict) and out.get("error"):
        raise RuntimeError(f"{tool}: {out['error']}")
    return out


def approx(a, b, tol=2e-3):
    return abs(a - b) < tol


async def main():
    checks = []

    def check(label, ok):
        checks.append((label, bool(ok)))

    elem = (await call("cad_part_studio_create", name="angle bracket (example)"))["elementId"]

    # variables — the part's public parameters
    for n, v in [("leg", f"{LEG} in"), ("width", f"{WIDTH} in"),
                 ("thick", f"{THICK} in"), ("hole_dia", f"{HOLE_DIA} in")]:
        await call("cad_set_variable", elementId=elem, name=n, expression=v)

    # L profile, grounded to origin, then dimensioned to the variables -> fully defined
    beg = await call("cad_sketch_begin", elementId=elem, plane="Front", name="L profile")
    sid = beg["sessionId"]
    pts = [[0, 0], [LEG, 0], [LEG, THICK], [THICK, THICK], [THICK, LEG], [0, LEG]]
    ln = (await call("cad_sketch_polyline", elementId=elem, sessionId=sid, points=pts))["lineIds"]
    # drive the two outer legs to #leg and the two wall thicknesses to #thick
    await call("cad_sketch_dimension", elementId=elem, sessionId=sid, kind="length", entity=ln[0], value="#leg")
    await call("cad_sketch_dimension", elementId=elem, sessionId=sid, kind="length", entity=ln[5], value="#leg")
    await call("cad_sketch_dimension", elementId=elem, sessionId=sid, kind="length", entity=ln[1], value="#thick")
    await call("cad_sketch_dimension", elementId=elem, sessionId=sid, kind="length", entity=ln[4], value="#thick")
    close = await call("cad_sketch_close", elementId=elem, sessionId=sid)
    check("sketch grounded + dimensioned (well-formed)", close.get("wellFormed"))

    # body
    ext = await call("cad_extrude", elementId=elem, sketchFeatureId=close["sketchFeatureId"],
                     depth="#width", operation="NEW", name="body")

    # measure #1: the L should be leg x width x leg (X x Y x Z)
    m1 = await call("cad_measure", elementId=elem)
    sx, sy, sz = m1["bbox"]["size"]
    check("body: single solid", m1["solidCount"] == 1)
    check("body: X extent == #leg", approx(sx, LEG))
    check("body: Y extent == #width", approx(sy, WIDTH))
    check("body: Z extent == #leg", approx(sz, LEG))

    # semantic selection -> fillet the inner concave corner (radius derived from #thick)
    concave = await call("cad_find_edges", elementId=elem, kind="concave")
    check("found the inner concave edge", len(concave["edgeIds"]) >= 1)
    if concave["edgeIds"]:
        await call("cad_fillet", elementId=elem, edgeIds=concave["edgeIds"],
                   radius="#thick * 0.8", name="inner fillet")

    # two holes up the vertical leg, drilled -Y through #width. NOTE: the idiomatic way to get
    # repeated holes is multiple centers in ONE cad_hole (the pattern lives in the sketch), not a
    # feature-pattern of the cut — feature-patterning a subtractive feature errors (see PLAN.md).
    hole = await call("cad_hole", elementId=elem, plane="Front",
                      centers=[[THICK / 2, 1.0], [THICK / 2, 1.6]],
                      diameter="#hole_dia", depth=2.0, name="holes")
    check("holes cut (OK)", hole.get("status") == "OK")

    # measure #2: still one solid, and material was removed by the holes (volume dropped)
    m2 = await call("cad_measure", elementId=elem)
    check("after holes: still one solid", m2["solidCount"] == 1)
    check("after holes: volume decreased", m2["volume"] < m1["volume"])

    # get_variables: the public parameter set reads back
    gv = await call("cad_get_variables", elementId=elem)
    names = {v["name"] for v in gv["variables"]}
    check("variables readable (leg/width/thick/hole_dia)",
          {"leg", "width", "thick", "hole_dia"} <= names)

    # THE decisive proof: change leg 2 -> 2.5 and the solid must grow to match
    await call("cad_set_variable", elementId=elem, name="leg", expression="2.5 in")
    m3 = await call("cad_measure", elementId=elem)
    gx, _, gz = m3["bbox"]["size"]
    check("edit #leg -> 2.5: X extent followed", approx(gx, 2.5))
    check("edit #leg -> 2.5: Z extent followed", approx(gz, 2.5))

    # export the finished part
    exp = await call("cad_export", elementId=elem, format="STEP")
    check("export STEP returned", bool(exp) and "error" not in exp)

    print("=== ANGLE BRACKET — full-part integration test ===")
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"\nmeasure#1 (body)        {json.dumps(m1)}")
    print(f"measure#2 (with holes)  {json.dumps(m2)}")
    print(f"measure#3 (leg -> 2.5)  {json.dumps(m3)}")
    print(f"element  https://cad.onshape.com/documents/{DOC}/w/{WS}/e/{elem}")
    n_pass = sum(ok for _, ok in checks)
    print(f"\nRESULT: {n_pass}/{len(checks)} checks  ->",
          "PASS ✅" if n_pass == len(checks) else "FAIL ❌")


if __name__ == "__main__":
    asyncio.run(main())
