"""On-demand live smoke for the P3 sketch fillet + adjacency finder.

ONE build proves both:
  - `cad_sketch_fillet`: a 2x2 square with a 0.5in fillet on one corner closes a region and
    extrudes to a single solid. The fillet's signature is exact — a sharp 2x2x1 box is 4.0 in^3;
    rounding one corner removes r^2(1 - pi/4)*depth = 0.0536 in^3, so volume ~= 3.9464. If the
    fillet failed to close the loop (or the dropped corner-coincident regressed) we'd see 0 solids
    or a regen error, not this number.
  - `cad_find_faces kind=adjacent_to_extreme`: the faces bordering the top (+Z) face. This is the
    call that SETTLES the unverified `qAdjacent(query, AdjacencyType.EDGE, EntityType.FACE)`
    signature — a wrong signature returns [] or an FS error; a right one returns the side faces
    (>=4; ~5 with the rounded corner's cylindrical face).

Reuses the existing test doc (no new document). Budget: ~5-6 successful API calls.
Run:  venv/bin/python -m scripts.smoke_fillet_adjacency   (or: venv/bin/python scripts/smoke_fillet_adjacency.py)
"""
import asyncio
import json

from cadkit_mcp import server as S

DOC = "550b17de256c3edec2066d48"
WS = "53187ebf4f6319f9815dc853"


async def call(tool, **a):
    a = {"documentId": DOC, "workspaceId": WS, **a}
    r = await S.dispatch(tool, a)
    try:
        return json.loads(r[0].text)
    except (json.JSONDecodeError, ValueError):
        return r[0].text


async def main():
    checks = []
    start = (await call("cad_api_calls")).get("session", 0)

    elem = (await call("cad_part_studio_create", name="smoke fillet+adjacency"))["elementId"]

    # 2x2 square on Top, fillet the bottom-right corner (where rectangle's bottom & right meet)
    beg = await call("cad_sketch_begin", elementId=elem, plane="Top", name="filleted square")
    sid = beg["sessionId"]
    rect = await call("cad_sketch_rectangle", elementId=elem, sessionId=sid,
                      corner1=[0, 0], corner2=[2, 2])
    fil = await call("cad_sketch_fillet", elementId=elem, sessionId=sid,
                     line1=rect["bottom"], line2=rect["right"], radius=0.5)
    checks.append(("fillet: arc + center computed", isinstance(fil, dict)
                   and abs(fil.get("center", [0, 0])[0] - 1.5) < 1e-6
                   and abs(fil.get("center", [0, 0])[1] - 0.5) < 1e-6))
    close = await call("cad_sketch_close", elementId=elem, sessionId=sid)
    await call("cad_extrude", elementId=elem, sketchFeatureId=close["sketchFeatureId"],
               depth="1 in", operation="NEW", name="block")

    # measure: 1 solid (region closed) and volume ~= 3.9464 (corner actually rounded)
    m = await call("cad_measure", elementId=elem)
    checks.append(("measure: 1 solid (fillet closed the region)", m.get("solidCount") == 1))
    vol = m.get("volume") or 0
    checks.append((f"measure: volume ~3.9464 (got {vol:.4f}; sharp box would be 4.0)",
                   abs(vol - 3.9464) < 5e-3))

    # adjacency: faces bordering the top (+Z) face -> the vertical sides (>=4)
    try:
        adj = await call("cad_find_faces", elementId=elem, kind="adjacent_to_extreme", axis="Z", max=True)
        face_ids = adj.get("faceIds", []) if isinstance(adj, dict) else []
        checks.append((f"adjacency: qAdjacent returned the side faces (got {len(face_ids)}, want >=4) "
                       f"-> SIGNATURE {'CONFIRMED' if len(face_ids) >= 4 else 'WRONG (empty)'}",
                       len(face_ids) >= 4))
    except Exception as e:                       # FS compile/runtime error => signature is wrong
        checks.append((f"adjacency: qAdjacent SIGNATURE WRONG (FS error: {str(e)[:80]})", False))

    spent = (await call("cad_api_calls")).get("session", 0) - start
    print()
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"\n  successful API calls this smoke: {spent}")
    print("  " + ("ALL PASS" if all(ok for _, ok in checks) else "SOME FAILED"))


if __name__ == "__main__":
    asyncio.run(main())
