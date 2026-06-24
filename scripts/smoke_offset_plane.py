"""On-demand live smoke for cad_plane (OFFSET datum plane).

Proves the new feature end-to-end: create a construction plane offset 2" above the Top
plane, resolve its deterministic id, open a sketch ON that plane, extrude, and confirm the
solid actually sits at the offset height (its min-Z ≈ the offset). That last check is the
real proof — it can only pass if the sketch truly lived on the datum plane, not the origin.

The two things offline can't verify and this nails:
  1. The cPlane param ids / enum (cplaneType=CPlaneType OFFSET, offset) regenerate OK.
  2. qCreatedBy(makeId(fid), EntityType.FACE) returns the plane's face — a sketch can
     target it (the BODY id is rejected as a sketch plane; live-confirmed).

Build: offset plane (Top + 2") -> 1x1 sketch on it -> extrude 0.25 up.
Expect: plane status OK, a planeId came back, solid count 1, min-Z ≈ 2.0 (not 0).

Budget: ~6 successful API calls. Fresh part studio in the existing test doc.
"""
import asyncio
import json

from cadkit_mcp import server as S
from cadkit_mcp.devkit import measure_fs, parse_fs

DOC = "550b17de256c3edec2066d48"
WS = "53187ebf4f6319f9815dc853"
OFFSET = 2.0


async def call(tool, **a):
    a = {"documentId": DOC, "workspaceId": WS, **a}
    r = await S.dispatch(tool, a)
    return json.loads(r[0].text)


async def main():
    log = []

    # 1. fresh part studio
    ps = await call("cad_part_studio_create", name="smoke offset plane")
    elem = ps["elementId"]
    log.append(("part_studio", ps))

    # 2. an OFFSET datum plane, 2" above Top
    plane = await call("cad_plane", elementId=elem, reference="Top", offset=OFFSET, name="lift")
    plane_id = plane.get("planeId")
    log.append(("plane", plane))

    # 3. a 1x1 sketch ON the datum plane (pass the resolved id as `face`)
    beg = await call("cad_sketch_begin", elementId=elem, face=plane_id, name="onplane")
    sid = beg["sessionId"]
    await call("cad_sketch_rectangle", elementId=elem, sessionId=sid,
               corner1=[0.0, 0.0], corner2=[1.0, 1.0])
    close = await call("cad_sketch_close", elementId=elem, sessionId=sid)
    ext = await call("cad_extrude", elementId=elem, sketchFeatureId=close["sketchFeatureId"],
                     depth=0.25, operation="NEW", name="pad")
    log.append(("extrude", ext))

    # 4. one eval: where does the solid sit? min-Z should be ~OFFSET, proving the sketch
    #    lived on the lifted plane (Top sketches map sketch-plane to world Z).
    res = await S.FS.evaluate(DOC, WS, elem, measure_fs())
    m = parse_fs(res.get("result", res))
    log.append(("measure", m))

    print("=== STEP RESULTS ===")
    for name, r in log:
        print(f"{name:14} {json.dumps(r)[:180]}")

    print("\n=== VERDICT ===")
    plane_ok = plane.get("status") == "OK"
    have_id = bool(plane_id)
    count = m.get("solidCount")
    minz = (m.get("solidMin") or [None, None, None])[2]
    z_ok = minz is not None and abs(minz - OFFSET) < 0.05
    print(f"plane status   : {plane.get('status')}   ({'PASS' if plane_ok else 'FAIL'})")
    print(f"planeId        : {plane_id}   ({'PASS' if have_id else 'FAIL'})")
    print(f"solid count    : {count}   (expect 1)")
    print(f"solid min-Z    : {minz}   (expect ~{OFFSET} => sketch sat on the offset plane)")
    print(f"element        : https://cad.onshape.com/documents/{DOC}/w/{WS}/e/{elem}")
    ok = plane_ok and have_id and count == 1 and z_ok
    print("\nSMOKE:", "PASS ✅" if ok else "FAIL ❌")


if __name__ == "__main__":  # never run on import — only on demand
    asyncio.run(main())
