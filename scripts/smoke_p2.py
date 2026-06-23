"""On-demand live smoke for P2 (inspection / lifecycle / I/O).

Drives the real dispatch end-to-end and proves each tool by its EFFECT, not just a
200 response: a variable's expression reads back, an edit changes measured volume, a
suppress drops the solid count to 0, an export returns a translation. Reuses the
existing test doc (no new document). Budget: ~12 successful API calls.
"""
import asyncio
import json

from cadkit_mcp import server as S

DOC = "550b17de256c3edec2066d48"
WS = "53187ebf4f6319f9815dc853"


async def call(tool, **a):
    a = {"documentId": DOC, "workspaceId": WS, **a}
    r = await S.dispatch(tool, a)
    return json.loads(r[0].text)


async def main():
    checks = []

    elem = (await call("cad_part_studio_create", name="smoke P2"))["elementId"]

    # variable + a 1in x 1in x #w box
    await call("cad_set_variable", elementId=elem, name="w", expression="0.5 in")
    beg = await call("cad_sketch_begin", elementId=elem, plane="Top", name="box")
    await call("cad_sketch_rectangle", elementId=elem, sessionId=beg["sessionId"],
               corner1=[0.5, 0.5], corner2=[1.5, 1.5])
    close = await call("cad_sketch_close", elementId=elem, sessionId=beg["sessionId"])
    box = await call("cad_extrude", elementId=elem, sketchFeatureId=close["sketchFeatureId"],
                     depth="#w", operation="NEW", name="box")
    box_fid = box["featureId"]

    # cad_measure: 1 solid, volume ~0.5 in^3 (1*1*0.5)
    m1 = await call("cad_measure", elementId=elem)
    checks.append(("measure: 1 solid", m1.get("solidCount") == 1))
    checks.append(("measure: volume ~0.5", abs((m1.get("volume") or 0) - 0.5) < 1e-6))

    # cad_get_variables: w reads back as "0.5 in"
    gv = await call("cad_get_variables", elementId=elem)
    w = next((v for v in gv["variables"] if v["name"] == "w"), None)
    checks.append(("get_variables: w == '0.5 in'", w and w["expression"] == "0.5 in"))

    # cad_edit_feature: retarget the extrude depth to a literal 1 in -> volume doubles
    await call("cad_edit_feature", elementId=elem, featureId=box_fid,
               parameterId="depth", expression="1 in")
    m2 = await call("cad_measure", elementId=elem)
    checks.append(("edit_feature: volume -> ~1.0", abs((m2.get("volume") or 0) - 1.0) < 1e-6))

    # cad_suppress: drop the box -> 0 solids
    await call("cad_suppress", elementId=elem, featureId=box_fid, suppressed=True)
    m3 = await call("cad_measure", elementId=elem)
    checks.append(("suppress: 0 solids", m3.get("solidCount") == 0))
    await call("cad_suppress", elementId=elem, featureId=box_fid, suppressed=False)  # restore for export

    # cad_export: STEP translation returned
    exp = await call("cad_export", elementId=elem, format="STEP")
    checks.append(("export: returned a result", bool(exp) and "error" not in exp))

    # cad_delete_feature: removes it
    dele = await call("cad_delete_feature", elementId=elem, featureId=box_fid)
    checks.append(("delete_feature: ok", dele.get("status") == "deleted"))

    print("=== P2 SMOKE ===")
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"\nmeasure#1 {json.dumps(m1)}")
    print(f"measure#2 {json.dumps(m2)}")
    print(f"export    {json.dumps(exp)[:200]}")
    print(f"element   https://cad.onshape.com/documents/{DOC}/w/{WS}/e/{elem}")
    print("\nP2 SMOKE:", "PASS ✅" if all(ok for _, ok in checks) else "FAIL ❌")


if __name__ == "__main__":
    asyncio.run(main())
