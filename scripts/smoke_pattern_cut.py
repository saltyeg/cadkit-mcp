"""On-demand live smoke for patterning a CUT (cad_pattern operation="REMOVE").

Regression for the fixed open bug: a feature-pattern of a REMOVE hole used to error and
leak a stray body because the builder hardcoded operationType=NEW. This drives the REAL
tool dispatch end-to-end and proves the REMOVE path REGENERATES (status OK) and cuts at
each instance WITHOUT spawning extra solids.

Build: one box (1 solid) → one REMOVE hole near one end → linear pattern of the hole,
count 3, operation="REMOVE". Expect: pattern status OK, solid count still 1 (three holes
in one body — NOT 1 box + stray bodies).

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

    # 1. fresh part studio
    ps = await call("cad_part_studio_create", name="smoke pattern cut")
    elem = ps["elementId"]
    log.append(("part_studio", ps))

    # 2. a box: 3 x 1 x 0.5, corner at origin
    beg = await call("cad_sketch_begin", elementId=elem, plane="Top", name="box")
    sid = beg["sessionId"]
    await call("cad_sketch_rectangle", elementId=elem, sessionId=sid,
               corner1=[0.0, 0.0], corner2=[3.0, 1.0])
    close = await call("cad_sketch_close", elementId=elem, sessionId=sid)
    ext = await call("cad_extrude", elementId=elem, sketchFeatureId=close["sketchFeatureId"],
                     depth=0.5, operation="NEW", name="box")
    box_fid = ext["featureId"]
    log.append(("extrude", ext))

    # 3. one REMOVE hole near the left end (a simple-style cut: circles + REMOVE extrude)
    hole = await call("cad_hole", elementId=elem, style="simple", plane="Top",
                      diameter=0.25, depth=0.5, centers=[[0.5, 0.5]], name="hole")
    hole_fid = hole["featureId"]
    log.append(("hole", hole))

    # 4. an X-axis edge to drive the pattern direction
    edges = await call("cad_find_edges", elementId=elem, kind="linear", axis="X")
    dir_edge = edges["edgeIds"][0]

    # 5. pattern the hole along X, count 3, operation=REMOVE  -> cuts 2 more holes, no new body
    pat = await call("cad_pattern", elementId=elem, kind="linear", featureIds=[hole_fid],
                     directionId=dir_edge, spacing=1.0, count=3, operation="REMOVE", name="holes")
    log.append(("pattern", pat))

    # 6. one eval: solid count should still be 1 (a single box with three holes)
    res = await S.FS.evaluate(DOC, WS, elem, measure_fs())
    m = parse_fs(res.get("result", res))
    log.append(("measure", m))

    print("=== STEP RESULTS ===")
    for name, r in log:
        print(f"{name:14} {json.dumps(r)[:160]}")
    print("\n=== VERDICT ===")
    pat_ok = pat.get("status") == "OK"
    count = m.get("solidCount")
    print(f"pattern status : {pat.get('status')}   ({'PASS' if pat_ok else 'FAIL'})")
    print(f"solid count    : {count}   (one box, three holes => expect 1, NOT >1 stray bodies)")
    print(f"element        : https://cad.onshape.com/documents/{DOC}/w/{WS}/e/{elem}")
    ok = pat_ok and count == 1
    print("\nSMOKE:", "PASS ✅" if ok else "FAIL ❌")


if __name__ == "__main__":  # never run on import — only on demand
    asyncio.run(main())
