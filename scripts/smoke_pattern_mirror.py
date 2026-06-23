"""On-demand live smoke for feature-based cad_pattern / cad_mirror.

Drives the REAL tool dispatch (not the builders directly) end-to-end, then measures
the solid count in a single FeatureScript eval. Proves the reworked feature-based
mirror/pattern REGENERATE (status OK) and actually add bodies — the exact thing the
old face-based form failed to do.

Budget: ~7 successful API calls. Reuses the existing test doc (no new document).
"""
import asyncio
import json

from cadkit_mcp import server as S
from cadkit_mcp.devkit import measure_fs, parse_fs

DOC = "550b17de256c3edec2066d48"
WS = "53187ebf4f6319f9815dc853"


async def call(tool, **a):
    a = {"documentId": DOC, "workspaceId": WS, **a}
    r = await S.dispatch(tool, a)
    return json.loads(r[0].text)


async def main():
    log = []

    # 1. fresh part studio (don't touch the hand-built reference element)
    ps = await call("cad_part_studio_create", name="smoke pattern/mirror")
    elem = ps["elementId"]
    log.append(("part_studio", ps))

    # 2. a box offset from x=0 so the mirror across Right (x=0) makes a distinct body
    beg = await call("cad_sketch_begin", elementId=elem, plane="Top", name="box")
    sid = beg["sessionId"]
    await call("cad_sketch_rectangle", elementId=elem, sessionId=sid,
               corner1=[0.5, 0.5], corner2=[1.5, 1.5])
    close = await call("cad_sketch_close", elementId=elem, sessionId=sid)
    log.append(("sketch_close", close))

    ext = await call("cad_extrude", elementId=elem, sketchFeatureId=close["sketchFeatureId"],
                     depth=0.5, operation="NEW", name="box")
    log.append(("extrude", ext))
    box_fid = ext["featureId"]
    assert box_fid, "extrude did not return a featureId"

    # 3. an X-axis edge of the box to drive the linear pattern direction
    edges = await call("cad_find_edges", elementId=elem, kind="linear", axis="X")
    dir_edge = edges["edgeIds"][0]
    log.append(("find_edges", edges))

    # 4. feature-based LINEAR pattern of the box: count 2, 3in apart  -> +1 body
    pat = await call("cad_pattern", elementId=elem, kind="linear", featureIds=[box_fid],
                     directionId=dir_edge, spacing=3, count=2, name="lin")
    log.append(("pattern", pat))

    # 5. feature-based MIRROR of the box across Right (x=0)            -> +1 body
    mir = await call("cad_mirror", elementId=elem, featureIds=[box_fid],
                     planeId="Right", name="mir")
    log.append(("mirror", mir))

    # 6. one eval: how many solids exist now? (unwrap the eval-response envelope)
    res = await S.FS.evaluate(DOC, WS, elem, measure_fs())
    m = parse_fs(res.get("result", res))
    log.append(("measure", m))

    print("=== STEP RESULTS ===")
    for name, r in log:
        print(f"{name:14} {json.dumps(r)[:160]}")
    print("\n=== VERDICT ===")
    pat_ok = pat.get("status") == "OK"
    mir_ok = mir.get("status") == "OK"
    count = m.get("solidCount")
    print(f"pattern status : {pat.get('status')}   ({'PASS' if pat_ok else 'FAIL'})")
    print(f"mirror status  : {mir.get('status')}   ({'PASS' if mir_ok else 'FAIL'})")
    print(f"solid count    : {count}   (1 box + pattern copy + mirror copy => expect 3)")
    print(f"element        : https://cad.onshape.com/documents/{DOC}/w/{WS}/e/{elem}")
    ok = pat_ok and mir_ok and (count or 0) >= 3
    print("\nSMOKE:", "PASS ✅" if ok else "FAIL ❌")


if __name__ == "__main__":  # never run on import (e.g. pytest collection) — only on demand
    asyncio.run(main())
