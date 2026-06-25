"""On-demand live smoke for variable-driven center placement (P4-2) + point-on-curve.

Two things can't be proven offline; both verified here in ONE part studio to save calls:

Phase A — variable-driven center (kind="position"):
  1. a directional DISTANCE measured from the EXTERNAL origin vertex (IB) to a local point
     regenerates OK (Onshape accepts that reference + the DimensionDirection enum);
  2. editing the variable MOVES the geometry (the dimension drives, it isn't decorative).
  Build: vars #hx=2,#hy=1 -> circle r0.25 nominal (2,1), diam 0.5, position center to (#hx,#hy)
         -> extrude peg -> bbox center ~ (2,1) -> set #hx=3 -> center x ~ 3.

Phase B — point-on-curve coincident (the polar bolt-circle recipe):
  3. COINCIDENT(point, curve) puts a seed hole's center ON a #bcd-dimensioned construction
     circle and regenerates OK (no WARNING) — i.e. a bolt circle composes from circular
     pattern + a variable construction circle, no bespoke feature.
  Build: var #bcd=3 -> construction circle r1.5 (diam #bcd) + seed circle r0.2 at (1.5,0),
         coincident(seed.center, construction circle), circular-pattern x4 about origin
         -> extrude the 4 seeds NEW -> total solids = 1 peg + 4 = 5.

Budget: ~13 successful API calls. Fresh part studio in the existing test doc.
Run ONCE: on any error, read the step output below — do not blind-retry.

Live-verified 2026-06-25: peg landed at (3,1) after #hx 2->3 (the position dim drove it);
the 4 bolt pegs sat exactly on the r1.5 (#bcd=3) circle at 0/90/180/270deg; all features OK.
"""
import asyncio
import json
import traceback

from cadkit_mcp import server as S
from cadkit_mcp.devkit import measure_fs, parse_fs

DOC = "550b17de256c3edec2066d48"
WS = "53187ebf4f6319f9815dc853"


async def call(tool, **a):
    r = await S.dispatch(tool, {"documentId": DOC, "workspaceId": WS, **a})
    txt = r[0].text
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return txt


async def measure(elem):
    res = await S.FS.evaluate(DOC, WS, elem, measure_fs())
    return parse_fs(res.get("result", res))


def center_of(m):
    # measure_fs emits solidMin/solidMax (parse_fs returns them verbatim — NOT a nested bbox dict)
    lo, hi = m.get("solidMin"), m.get("solidMax")
    return [(lo[i] + hi[i]) / 2 for i in range(3)] if lo and hi else None


def near(a, b, tol=0.05):
    return a is not None and abs(a - b) < tol


async def main():
    ps = await call("cad_part_studio_create", name="smoke variable center + point-on-curve")
    elem = ps["elementId"]
    print("element :", f"https://cad.onshape.com/documents/{DOC}/w/{WS}/e/{elem}")

    # ---- Phase A : variable-driven center ----------------------------------
    a_status = a_c1 = a_c2 = None
    try:
        await call("cad_set_variable", elementId=elem, name="hx", expression="2 in")
        await call("cad_set_variable", elementId=elem, name="hy", expression="1 in")
        beg = await call("cad_sketch_begin", elementId=elem, plane="Top", name="peg")
        sid = beg["sessionId"]
        cir = await call("cad_sketch_circle", elementId=elem, sessionId=sid, center=[2.0, 1.0], radius=0.25)
        await call("cad_sketch_dimension", elementId=elem, sessionId=sid, kind="diameter",
                   entity=cir["entityId"], value=0.5)
        await call("cad_sketch_dimension", elementId=elem, sessionId=sid, kind="position",
                   entity=f"{cir['entityId']}.center", value=["#hx", "#hy"])
        close = await call("cad_sketch_close", elementId=elem, sessionId=sid)
        ext = await call("cad_extrude", elementId=elem, sketchFeatureId=close["sketchFeatureId"],
                         depth=0.25, operation="NEW", name="peg")
        a_status = (close.get("status"), ext.get("status"))
        a_c1 = center_of(await measure(elem))
        await call("cad_set_variable", elementId=elem, name="hx", expression="3 in")
        a_c2 = center_of(await measure(elem))
        print("PHASE A  close/extrude:", a_status, "| center@hx=2:", a_c1, "| center@hx=3:", a_c2)
    except Exception:
        print("PHASE A raised:\n", traceback.format_exc())

    # ---- Phase B : point-on-curve coincident (bolt-circle recipe) ----------
    b_status = b_count = None
    try:
        await call("cad_set_variable", elementId=elem, name="bcd", expression="3 in")
        beg = await call("cad_sketch_begin", elementId=elem, plane="Top", name="boltcircle")
        sid = beg["sessionId"]
        con = await call("cad_sketch_circle", elementId=elem, sessionId=sid,
                         center=[0.0, 0.0], radius=1.5, construction=True)
        await call("cad_sketch_dimension", elementId=elem, sessionId=sid, kind="diameter",
                   entity=con["entityId"], value="#bcd")
        seed = await call("cad_sketch_circle", elementId=elem, sessionId=sid, center=[1.5, 0.0], radius=0.2)
        await call("cad_sketch_dimension", elementId=elem, sessionId=sid, kind="diameter",
                   entity=seed["entityId"], value=0.4)
        # the key step: seed center ON the construction circle (point-on-curve coincident)
        await call("cad_sketch_constrain", elementId=elem, sessionId=sid,
                   type="coincident", a=f"{seed['entityId']}.center", b=con["entityId"])
        await call("cad_sketch_pattern", elementId=elem, sessionId=sid,
                   entityIds=[seed["entityId"]], kind="circular", count=4, center=[0.0, 0.0], angle=90.0)
        close = await call("cad_sketch_close", elementId=elem, sessionId=sid)
        ext = await call("cad_extrude", elementId=elem, sketchFeatureId=close["sketchFeatureId"],
                         depth=0.25, operation="NEW", name="boltpegs")
        b_status = (close.get("status"), ext.get("status"))
        b_count = (await measure(elem)).get("solidCount")
        print("PHASE B  close/extrude:", b_status, "| total solids:", b_count, "(expect 5)")
    except Exception:
        print("PHASE B raised:\n", traceback.format_exc())

    # ---- verdict -----------------------------------------------------------
    print("\n=== VERDICT ===")
    A1 = a_status == ("OK", "OK")
    A2 = a_c1 and near(a_c1[0], 2) and near(a_c1[1], 1)
    A3 = a_c2 and near(a_c2[0], 3) and near(a_c2[1], 1)
    B1 = b_status == ("OK", "OK")
    B2 = b_count == 5
    print(f"A  origin-vertex directional dist regen OK : {A1}  ({'PASS' if A1 else 'FAIL'})")
    print(f"A  placed at (#hx,#hy)=(2,1)              : {A2}  ({'PASS' if A2 else 'FAIL'})")
    print(f"A  editing #hx -> peg slid to x=3          : {A3}  ({'PASS' if A3 else 'FAIL'})")
    print(f"B  point-on-curve coincident regen OK      : {B1}  ({'PASS' if B1 else 'FAIL'})")
    print(f"B  4 holes on the #bcd circle (5 solids)   : {B2}  ({'PASS' if B2 else 'FAIL'})")
    print("\nSMOKE:", "PASS ✅" if all([A1, A2, A3, B1, B2]) else "FAIL ❌")


if __name__ == "__main__":
    asyncio.run(main())
