"""On-demand live smoke for the live-linked in-sketch pattern (LINEAR/CIRCULAR_PATTERN).

cad_sketch_pattern on a single circle now emits the REAL sketch-pattern constraint (read back
from a UI ground truth), not loose geometric copies. This proves both types regenerate OK (the
construct + its reversed-per-type localInstance roles are valid) and create the right number of
instances.

Build (one part studio):
  - CIRCULAR: circle r0.2 at (1,0), pattern count 4 about origin every 90deg -> extrude -> 4 disks
  - LINEAR:   circle r0.2 at (0,3), pattern count 3 along +X spacing 1   -> extrude -> 3 disks
Expect: both sketches close OK (not WARNING/ERROR), both patterns report a constraintId (the
linked path, not geometric), total solid count = 4 + 3 = 7.

Budget: ~6 successful API calls. Fresh part studio in the existing test doc.
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


async def pattern_in_sketch(elem, name, plane, loc, kind, count, **patkw):
    beg = await call("cad_sketch_begin", elementId=elem, plane=plane, name=name)
    sid = beg["sessionId"]
    cir = await call("cad_sketch_circle", elementId=elem, sessionId=sid, center=loc, radius=0.2)
    await call("cad_sketch_dimension", elementId=elem, sessionId=sid, kind="diameter",
               entity=cir["entityId"], value=0.4)
    pat = await call("cad_sketch_pattern", elementId=elem, sessionId=sid,
                     entityIds=[cir["entityId"]], kind=kind, count=count, **patkw)
    close = await call("cad_sketch_close", elementId=elem, sessionId=sid)
    ext = await call("cad_extrude", elementId=elem, sketchFeatureId=close["sketchFeatureId"],
                     depth=0.25, operation="NEW", name=name)
    return pat, close, ext


async def main():
    ps = await call("cad_part_studio_create", name="smoke linked sketch pattern")
    elem = ps["elementId"]

    patC, closeC, extC = await pattern_in_sketch(
        elem, "circular", "Top", [1.0, 0.0], "circular", 4, center=[0.0, 0.0], angle=90.0)
    patL, closeL, extL = await pattern_in_sketch(
        elem, "linear", "Top", [0.0, 3.0], "linear", 3, direction=[1.0, 0.0], spacing=1.0)

    res = await S.FS.evaluate(DOC, WS, elem, measure_fs())
    m = parse_fs(res.get("result", res))
    count = m.get("solidCount")

    print("=== STEP RESULTS ===")
    print("circular: pattern linked?", "constraintId" in patC, "| close", closeC.get("status"), "| extrude", extC.get("status"))
    print("linear  : pattern linked?", "constraintId" in patL, "| close", closeL.get("status"), "| extrude", extL.get("status"))
    print("total solids:", count, "(expect 4 + 3 = 7)")

    print("\n=== VERDICT ===")
    linked = "constraintId" in patC and "constraintId" in patL
    statuses = [closeC.get("status"), closeL.get("status"), extC.get("status"), extL.get("status")]
    ok_status = all(st == "OK" for st in statuses)
    ok_count = count == 7
    print(f"both used linked construct : {linked}  ({'PASS' if linked else 'FAIL'})")
    print(f"all statuses OK            : {statuses}  ({'PASS' if ok_status else 'FAIL'})")
    print(f"solid count 7             : {count}  ({'PASS' if ok_count else 'FAIL'})")
    print(f"element                   : https://cad.onshape.com/documents/{DOC}/w/{WS}/e/{elem}")
    print("\nSMOKE:", "PASS ✅" if (linked and ok_status and ok_count) else "FAIL ❌")


if __name__ == "__main__":
    asyncio.run(main())
