"""On-demand live smoke for cad_sketch_mirror — settles the MIRROR constraint semantics.

Builds the RIGHT half of a diamond and mirrors it across the Y axis to close the full profile:
  (0,0) -> (1,1) -> (0,2), mirrored -> (-1,1), giving a rhombus 2 wide x 2 tall (area 2).
Extruded 1in that is a 2.0 in^3 solid, symmetric in X from -1 to +1.

What this proves:
  - reflected GEOMETRY is right  -> volume ~= 2.0 and the X bbox is symmetric (-1..+1)
  - the MIRROR constraint is ACCEPTED (not malformed) -> the sketch regenerates without ERROR;
    a bad parameterId set would make the whole sketch feature ERROR and the extrude would find
    no region (0 solids).

Reuses the existing test doc. Budget: ~5 successful API calls.
Run:  venv/bin/python scripts/smoke_sketch_mirror.py
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
    elem = (await call("cad_part_studio_create", name="smoke sketch mirror"))["elementId"]

    beg = await call("cad_sketch_begin", elementId=elem, plane="Top", name="half diamond")
    sid = beg["sessionId"]
    axis = (await call("cad_sketch_line", elementId=elem, sessionId=sid,
                       start=[0, 0], end=[0, 3], construction=True))["entityId"]
    l1 = (await call("cad_sketch_line", elementId=elem, sessionId=sid, start=[0, 0], end=[1, 1]))["entityId"]
    l2 = (await call("cad_sketch_line", elementId=elem, sessionId=sid, start=[1, 1], end=[0, 2]))["entityId"]
    await call("cad_sketch_constrain", elementId=elem, sessionId=sid, type="coincident",
               a=f"{l1}.end", b=f"{l2}.start")
    mapping = await call("cad_sketch_mirror", elementId=elem, sessionId=sid, entityIds=[l1, l2], axis=axis)
    checks.append(("mirror returned a copy per line", isinstance(mapping, dict) and len(mapping) == 2))
    # pin the four corners so the loop is closed regardless of region auto-detection
    await call("cad_sketch_constrain", elementId=elem, sessionId=sid, type="coincident",
               a=f"{l1}.start", b=f"{mapping[l1]}.start")
    await call("cad_sketch_constrain", elementId=elem, sessionId=sid, type="coincident",
               a=f"{l2}.end", b=f"{mapping[l2]}.end")

    close = await call("cad_sketch_close", elementId=elem, sessionId=sid)
    status = close.get("status") if isinstance(close, dict) else None
    checks.append((f"sketch regenerated without ERROR (status={status}) -> MIRROR constraint accepted",
                   status in (None, "OK", "INFO", "WARNING")))
    await call("cad_extrude", elementId=elem, sketchFeatureId=close["sketchFeatureId"],
               depth="1 in", operation="NEW", name="diamond")

    m = await call("cad_measure", elementId=elem)
    checks.append(("measure: 1 solid (mirror closed the profile)", m.get("solidCount") == 1))
    vol = m.get("volume") or 0
    checks.append((f"measure: volume ~2.0 (got {vol:.4f})", abs(vol - 2.0) < 5e-3))
    mn, mx = m.get("bbox", {}).get("min", [0, 0, 0]), m.get("bbox", {}).get("max", [0, 0, 0])
    checks.append((f"measure: X bbox symmetric ~[-1,1] (got [{mn[0]:.3f},{mx[0]:.3f}])",
                   abs(mn[0] + 1) < 5e-3 and abs(mx[0] - 1) < 5e-3))

    spent = (await call("cad_api_calls")).get("session", 0) - start
    print()
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"\n  successful API calls this smoke: {spent}")
    print("  " + ("ALL PASS" if all(ok for _, ok in checks) else "SOME FAILED"))


if __name__ == "__main__":
    asyncio.run(main())
