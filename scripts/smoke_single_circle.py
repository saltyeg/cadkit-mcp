"""On-demand live smoke for the single-curve circle refactor.

add_circle now emits one native BTMSketchCurve-4 (was two semicircle arcs). This proves the
new form dimensions and extrudes to a CLEAN ROUND solid — the regression that mattered was the
old 2-arc "teardrop" bore (one diameter dim bound only the .a arc), which would show up as the
wrong volume and an asymmetric bbox.

Build: a circle drawn at the wrong radius, dimensioned diameter=1.0 -> extrude 0.5.
Expect: status OK (not WARNING), 1 solid, volume ~= pi*0.5^2*0.5 = 0.3927 in^3, bbox x==y==1.0
(round, not lopsided).

Budget: ~4 successful API calls. Fresh part studio in the existing test doc.
"""
import asyncio
import json
import math

from cadkit_mcp import server as S
from cadkit_mcp.devkit import measure_fs, parse_fs

DOC = "550b17de256c3edec2066d48"
WS = "53187ebf4f6319f9815dc853"
DIA = 1.0
THICK = 0.5


async def call(tool, **a):
    r = await S.dispatch(tool, {"documentId": DOC, "workspaceId": WS, **a})
    txt = r[0].text
    try:
        return json.loads(txt)        # feature/finder tools return JSON
    except json.JSONDecodeError:
        return txt                    # local session ops return a plain "ok"


async def main():
    ps = await call("cad_part_studio_create", name="smoke single circle")
    elem = ps["elementId"]

    beg = await call("cad_sketch_begin", elementId=elem, plane="Top", name="disk")
    sid = beg["sessionId"]
    cir = await call("cad_sketch_circle", elementId=elem, sessionId=sid, center=[0.0, 0.0], radius=2.0)
    await call("cad_sketch_dimension", elementId=elem, sessionId=sid, kind="diameter",
               entity=cir["entityId"], value=DIA)
    # ground the center to the origin — also proves `{cid}.center` is referenceable via centerId
    await call("cad_sketch_constrain", elementId=elem, sessionId=sid, type="ground_origin",
               a=f"{cir['entityId']}.center")
    close = await call("cad_sketch_close", elementId=elem, sessionId=sid)
    ext = await call("cad_extrude", elementId=elem, sketchFeatureId=close["sketchFeatureId"],
                     depth=THICK, operation="NEW", name="disk")

    res = await S.FS.evaluate(DOC, WS, elem, measure_fs())
    m = parse_fs(res.get("result", res))

    count = m.get("solidCount")
    vol = m.get("solidVolume")
    lo, hi = m.get("solidMin"), m.get("solidMax")
    sx = (hi[0] - lo[0]) if lo and hi else None
    sy = (hi[1] - lo[1]) if lo and hi else None
    want_vol = math.pi * (DIA / 2) ** 2 * THICK

    print("=== STEP RESULTS ===")
    print("sketch close:", close.get("status"), "| extrude:", ext.get("status"))
    print(f"solidCount={count} volume={vol} bbox x={sx} y={sy}")
    print("\n=== VERDICT ===")
    ok_status = close.get("status") == "OK" and ext.get("status") == "OK"
    ok_count = count == 1
    ok_vol = vol is not None and abs(vol - want_vol) < 0.01
    ok_round = sx is not None and abs(sx - DIA) < 0.01 and abs(sy - DIA) < 0.01
    print(f"status OK (not WARNING) : {close.get('status')}/{ext.get('status')}  ({'PASS' if ok_status else 'FAIL'})")
    print(f"single solid            : {count}  ({'PASS' if ok_count else 'FAIL'})")
    print(f"volume round disk       : {vol} vs {want_vol:.4f}  ({'PASS' if ok_vol else 'FAIL'})")
    print(f"bbox symmetric (round)  : x={sx} y={sy}  ({'PASS' if ok_round else 'FAIL'} — teardrop would be asymmetric)")
    print(f"element                 : https://cad.onshape.com/documents/{DOC}/w/{WS}/e/{elem}")
    print("\nSMOKE:", "PASS ✅" if (ok_status and ok_count and ok_vol and ok_round) else "FAIL ❌")


if __name__ == "__main__":
    asyncio.run(main())
