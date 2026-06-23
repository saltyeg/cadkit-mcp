"""On-demand live smoke: composite counterbore via cad_hole(style='counterbore').

A counterbore = the bore (narrow, full depth) + a wider, shallow recess, both cut on the
same plane. Verifies by reading the cylindrical-face radii: a counterbore must show TWO
distinct radii (bore + cbore). Budget: ~5 successful API calls.
"""
import asyncio
import json

from cadkit_mcp import server as S
from cadkit_mcp.devkit import parse_fs

DOC = "550b17de256c3edec2066d48"
WS = "53187ebf4f6319f9815dc853"

BORE_DIA, CBORE_DIA, CBORE_DEPTH = 0.25, 0.6, 0.3


async def call(tool, **a):
    r = await S.dispatch(tool, {"documentId": DOC, "workspaceId": WS, **a})
    t = r[0].text
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return t


async def main():
    elem = (await call("cad_part_studio_create", name="counterbore smoke"))["elementId"]
    # a block on the Front plane (extrudes -Y, so a Front-plane hole drills -Y into it)
    beg = await call("cad_sketch_begin", elementId=elem, plane="Front", name="block")
    await call("cad_sketch_rectangle", elementId=elem, sessionId=beg["sessionId"],
               corner1=[0, 0], corner2=[2, 2])
    close = await call("cad_sketch_close", elementId=elem, sessionId=beg["sessionId"])
    await call("cad_extrude", elementId=elem, sketchFeatureId=close["sketchFeatureId"],
               depth=1.0, operation="NEW", name="block")

    hole = await call("cad_hole", elementId=elem, plane="Front", centers=[[1, 1]],
                      diameter=BORE_DIA, depth=1.2, style="counterbore",
                      cboreDiameter=CBORE_DIA, cboreDepth=CBORE_DEPTH, name="CB")

    solid = "qBodyType(qEverything(EntityType.BODY),BodyType.SOLID)"
    fs = ('function(context is Context, queries){ var rs=[];'
          f'for (var f in evaluateQuery(context, qGeometry(qOwnedByBody({solid}, EntityType.FACE), GeometryType.CYLINDER)))'
          '{ rs=append(rs, roundToPrecision(evSurfaceDefinition(context,{"face":f}).radius/inch, 4)); }'
          f'return {{"cyl": rs, "solids": size(evaluateQuery(context, {solid}))}};}}')
    g = parse_fs((await S.FS.evaluate(DOC, WS, elem, fs)).get("result", {}))

    radii = sorted(set(g.get("cyl") or []))
    has_bore = any(abs(r - BORE_DIA / 2) < 1e-3 for r in radii)
    has_cbore = any(abs(r - CBORE_DIA / 2) < 1e-3 for r in radii)
    checks = [
        ("cad_hole bore OK", hole.get("status") == "OK"),
        ("cad_hole counterbore OK", hole.get("counterbore") == "OK"),
        ("still one solid", g.get("solids") == 1),
        (f"bore radius {BORE_DIA/2} present", has_bore),
        (f"counterbore radius {CBORE_DIA/2} present", has_cbore),
        ("two distinct radii (true counterbore)", len(radii) >= 2),
    ]
    print("=== COUNTERBORE SMOKE ===")
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"\ncad_hole -> {json.dumps(hole)}")
    print(f"cyl radii -> {radii}")
    print(f"element  https://cad.onshape.com/documents/{DOC}/w/{WS}/e/{elem}")
    print("\nSMOKE:", "PASS ✅" if all(ok for _, ok in checks) else "FAIL ❌")


if __name__ == "__main__":
    asyncio.run(main())
