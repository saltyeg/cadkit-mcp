"""On-demand live smoke for variable-driven center placement (P4-2).

`cad_sketch_dimension(kind="position", ...)` pins a sketch point to the part-studio origin with
directional (horizontal/vertical) DISTANCE dimensions whose values are #variables. The two things
that can't be proven offline:
  1. a directional DISTANCE measured from the EXTERNAL origin vertex (IB) to a local point
     actually regenerates OK (not WARNING) — i.e. Onshape accepts that reference + direction enum;
  2. editing the variable genuinely MOVES the geometry (the dimension drives, it isn't decorative).

Build (one part studio):
  - vars: #hx = 2 in, #hy = 1 in
  - sketch on Top: circle r0.25 at nominal (2,1); diameter dim 0.5; position its center to (#hx,#hy)
  - extrude 0.25 -> a peg
  - measure bbox center -> expect ~ (2, 1)
  - set #hx = 3 -> re-measure -> expect center x ~ 3 (the peg slid +1")

Expect: sketch + extrude status OK, center tracks the variables on both reads.

Budget: ~8 successful API calls. Fresh part studio in the existing test doc.
"""
import asyncio
import json

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


async def bbox_center(elem):
    res = await S.FS.evaluate(DOC, WS, elem, measure_fs())
    m = parse_fs(res.get("result", res))
    bb = m.get("bbox", {})
    lo, hi = bb.get("min"), bb.get("max")
    if not lo or not hi:
        return None
    return [(lo[i] + hi[i]) / 2 for i in range(3)]


async def main():
    ps = await call("cad_part_studio_create", name="smoke variable center")
    elem = ps["elementId"]

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

    c1 = await bbox_center(elem)
    await call("cad_set_variable", elementId=elem, name="hx", expression="3 in")
    c2 = await bbox_center(elem)

    print("=== STEP RESULTS ===")
    print("sketch status:", close.get("status"), "| extrude status:", ext.get("status"))
    print("center @ #hx=2:", c1, "(expect ~[2, 1, *])")
    print("center @ #hx=3:", c2, "(expect ~[3, 1, *] — slid +1 in X)")

    def near(a, b, tol=0.05): return a is not None and abs(a - b) < tol
    print("\n=== VERDICT ===")
    ok_status = close.get("status") == "OK" and ext.get("status") == "OK"
    ok_place = c1 and near(c1[0], 2) and near(c1[1], 1)
    ok_drive = c2 and near(c2[0], 3) and near(c2[1], 1)
    print(f"origin-vertex directional dist regen OK : {ok_status}  ({'PASS' if ok_status else 'FAIL'})")
    print(f"placed at (#hx,#hy)=(2,1)              : {ok_place}  ({'PASS' if ok_place else 'FAIL'})")
    print(f"editing #hx -> peg slid to x=3          : {ok_drive}  ({'PASS' if ok_drive else 'FAIL'})")
    print(f"element : https://cad.onshape.com/documents/{DOC}/w/{WS}/e/{elem}")
    print("\nSMOKE:", "PASS ✅" if (ok_status and ok_place and ok_drive) else "FAIL ❌")


if __name__ == "__main__":
    asyncio.run(main())
