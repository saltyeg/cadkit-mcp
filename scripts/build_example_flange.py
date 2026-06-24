"""Full-part integration test / worked example: a variable-driven pipe flange.

Like build_example_bracket.py, this proves the cadkit tools COMPOSE into a whole
part the cadkit way (grounded, dimension-driven, semantic selection) and self-checks
by asserting MEASURED geometry against the variables. It deliberately exercises the
tools the bracket doesn't — and in particular the just-fixed path:

  * cad_pattern kind="circular" with operation="REMOVE"  ← the bolt circle (the fixed bug)
  * a cylindrical FACE used as the pattern axis            ← cad_find_faces(kind="cylindrical")
  * extrude ADD (the raised hub) on top of the base plate
  * cad_chamfer on the bore mouth, cad_fillet on a concave junction
  * semantic selection: cylindrical / circular / concave / largest

A flange is the canonical part for a circular bolt pattern, so it maps cleanly onto
the toolset. The central axis of a concentric part is NOT a solid edge, so the bolt
circle is patterned about the BORE's cylindrical face — the idiomatic way to give a
circular pattern an axis without a physical edge there.

Geometry (Top plane, sketch-X/Y -> world-X/Y, extrude along +Z):
  - base plate: disk of #od, thickness #thick           (z 0..#thick)
  - raised hub: boss of #hub_d, up to #thick + #hub_h   (z 0..#thick+#hub_h)
  - central bore: #bore_d through the whole stack
  - N_BOLTS bolt holes of #bolt_d on a BCD bolt circle, circular-patterned (REMOVE)

#od/#bore_d/#hub_d drive diameters; #thick/#hub_h drive heights; #bolt_d drives the
bolt hole. BCD and the bolt count are realized as a seed coordinate + pattern count
(the one part that's a placement choice, not a driven dimension).

Budget: ~40 successful API calls (run on demand, not in CI).
"""
import asyncio
import json

from cadkit_mcp import server as S

DOC = "550b17de256c3edec2066d48"
WS = "53187ebf4f6319f9815dc853"

OD, BORE, THICK = 5.0, 2.0, 0.5
HUB_D, HUB_H = 3.0, 0.6
BOLT_D, BCD, N_BOLTS = 0.5, 4.0, 4


async def call(tool, **a):
    a = {"documentId": DOC, "workspaceId": WS, **a}
    r = await S.dispatch(tool, a)
    text = r[0].text
    if text.startswith("ERROR"):
        raise RuntimeError(text)
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        return text  # constrain/dimension return "ok"
    if isinstance(out, dict) and out.get("error"):
        raise RuntimeError(f"{tool}: {out['error']}")
    return out


def approx(a, b, tol=3e-3):
    return abs(a - b) < tol


async def disk(elem, plane, dia_expr, dia_guess, name):
    """A grounded, fully-defined circle on `plane`: centered at origin, dia driven by an
    expression. Returns the closed sketch's featureId."""
    beg = await call("cad_sketch_begin", elementId=elem, plane=plane, name=name)
    sid = beg["sessionId"]
    cid = (await call("cad_sketch_circle", elementId=elem, sessionId=sid,
                      center=[0, 0], radius=dia_guess / 2))["entityId"]
    await call("cad_sketch_constrain", elementId=elem, sessionId=sid,
               type="ground_origin", a=f"{cid}.center")
    await call("cad_sketch_dimension", elementId=elem, sessionId=sid,
               kind="diameter", entity=cid, value=dia_expr)
    return await call("cad_sketch_close", elementId=elem, sessionId=sid, require_well_formed=True)


async def main():
    checks = []

    def check(label, ok):
        checks.append((label, bool(ok)))

    elem = (await call("cad_part_studio_create", name="pipe flange (example)"))["elementId"]

    # public parameters
    for n, v in [("od", f"{OD} in"), ("bore_d", f"{BORE} in"), ("thick", f"{THICK} in"),
                 ("hub_d", f"{HUB_D} in"), ("hub_h", f"{HUB_H} in"), ("bolt_d", f"{BOLT_D} in")]:
        await call("cad_set_variable", elementId=elem, name=n, expression=v)

    # 1. base plate: a grounded disk of #od extruded #thick
    plate_sk = await disk(elem, "Top", "#od", 1.0, "plate profile")
    check("plate sketch well-formed", plate_sk.get("wellFormed"))
    await call("cad_extrude", elementId=elem, sketchFeatureId=plate_sk["sketchFeatureId"],
               depth="#thick", operation="NEW", name="plate")
    m1 = await call("cad_measure", elementId=elem)
    sx, sy, sz = m1["bbox"]["size"]
    check("plate: 1 solid", m1["solidCount"] == 1)
    check("plate: OD == #od (X,Y)", approx(sx, OD) and approx(sy, OD))
    check("plate: height == #thick (Z)", approx(sz, THICK))

    # 2. raised hub: a #hub_d boss extruded ADD up to #thick + #hub_h
    hub_sk = await disk(elem, "Top", "#hub_d", 1.0, "hub profile")
    await call("cad_extrude", elementId=elem, sketchFeatureId=hub_sk["sketchFeatureId"],
               depth="#thick + #hub_h", operation="ADD", name="hub")
    m2 = await call("cad_measure", elementId=elem)
    check("after hub: still 1 solid (ADD merged)", m2["solidCount"] == 1)
    check("after hub: height == #thick + #hub_h", approx(m2["bbox"]["size"][2], THICK + HUB_H))
    check("after hub: volume grew", m2["volume"] > m1["volume"])

    # 3. central bore through the whole stack (simple hole = circle + REMOVE extrude, upward)
    await call("cad_hole", elementId=elem, plane="Top", centers=[[0, 0]],
               diameter="#bore_d", depth="#thick + #hub_h", name="bore")
    m3 = await call("cad_measure", elementId=elem)
    check("after bore: still 1 solid", m3["solidCount"] == 1)
    check("after bore: volume dropped", m3["volume"] < m2["volume"])

    # 4. one seed bolt hole on the +X bolt circle, through the plate
    seed = await call("cad_hole", elementId=elem, plane="Top", centers=[[BCD / 2, 0]],
                      diameter="#bolt_d", depth="#thick", name="bolt seed")
    check("bolt seed cut (OK)", seed.get("status") == "OK")

    # 5. the bore's cylindrical face is the pattern axis (no physical edge on the centerline)
    cyl = await call("cad_find_faces", elementId=elem, kind="cylindrical", radius=BORE / 2)
    check("found the bore cylindrical face", len(cyl["faceIds"]) >= 1)

    # 6. THE showcase: circular pattern of the bolt hole about that axis, operation=REMOVE
    #    (the just-fixed path — a feature-pattern of a cut must REMOVE, not spawn a stray body)
    pat = None
    if cyl["faceIds"]:
        pat = await call("cad_pattern", elementId=elem, kind="circular",
                         featureIds=[seed["featureId"]], axisId=cyl["faceIds"][0],
                         count=N_BOLTS, angle=360, operation="REMOVE", name="bolt circle")
        check("circular REMOVE pattern regenerates OK", pat.get("status") == "OK")
    m4 = await call("cad_measure", elementId=elem)
    check("after bolt circle: STILL 1 solid (no stray bodies)", m4["solidCount"] == 1)
    check("after bolt circle: volume dropped (holes cut)", m4["volume"] < m3["volume"])

    # 7. chamfer the bore mouth (circular edges at the bore radius)
    rims = await call("cad_find_edges", elementId=elem, kind="circular", radius=BORE / 2)
    check("found bore rim circular edges", len(rims["edgeIds"]) >= 1)
    if rims["edgeIds"]:
        ch = await call("cad_chamfer", elementId=elem, edgeIds=rims["edgeIds"],
                        distance=0.06, name="bore chamfer")
        check("bore chamfer OK", ch.get("status") == "OK")

    # 8. fillet the concave hub/plate junction
    concave = await call("cad_find_edges", elementId=elem, kind="concave")
    check("found concave junction edge(s)", len(concave["edgeIds"]) >= 1)
    if concave["edgeIds"]:
        fl = await call("cad_fillet", elementId=elem, edgeIds=concave["edgeIds"],
                        radius=0.08, name="hub fillet")
        check("hub fillet OK", fl.get("status") == "OK")

    # 9. a semantic-selection sanity pass (largest face = a flat plate face)
    largest = await call("cad_find_faces", elementId=elem, kind="largest")
    check("largest face resolves", len(largest["faceIds"]) == 1)

    # 10. parameters read back
    gv = await call("cad_get_variables", elementId=elem)
    names = {v["name"] for v in gv["variables"]}
    check("variables readable", {"od", "bore_d", "thick", "hub_d", "hub_h", "bolt_d"} <= names)

    # 11. THE decisive proof: grow #od 5 -> 6 and the plate must follow
    await call("cad_set_variable", elementId=elem, name="od", expression="6 in")
    m5 = await call("cad_measure", elementId=elem)
    gx, gy, _ = m5["bbox"]["size"]
    check("edit #od -> 6: X extent followed", approx(gx, 6.0))
    check("edit #od -> 6: Y extent followed", approx(gy, 6.0))
    check("edit #od -> 6: still 1 solid", m5["solidCount"] == 1)

    # 12. export
    exp = await call("cad_export", elementId=elem, format="STEP")
    check("export STEP returned", bool(exp) and "error" not in exp)

    print("=== PIPE FLANGE — full-part integration test ===")
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"\nmeasure#1 (plate)        {json.dumps(m1)}")
    print(f"measure#2 (+hub)         {json.dumps(m2)}")
    print(f"measure#3 (+bore)        {json.dumps(m3)}")
    print(f"measure#4 (+bolt circle) {json.dumps(m4)}")
    print(f"measure#5 (od -> 6)      {json.dumps(m5)}")
    if pat is not None:
        print(f"bolt-circle pattern      {json.dumps(pat)}")
    print(f"element  https://cad.onshape.com/documents/{DOC}/w/{WS}/e/{elem}")
    n_pass = sum(ok for _, ok in checks)
    print(f"\nRESULT: {n_pass}/{len(checks)} checks  ->",
          "PASS ✅" if n_pass == len(checks) else "FAIL ❌")


if __name__ == "__main__":
    asyncio.run(main())
