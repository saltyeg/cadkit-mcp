"""Integration build — a parametric HIGH-SPEED 6 mm D-shaft hub (metric).

Exercises the P4 feature set end-to-end on one real part:
  - D-bore as a true D profile (major arc + chord), the flat located by a *horizontal*
    distance dimension to the axis  -> dim_distance(direction="horizontal")
  - mounting bolt circle: construction circle (#bcd) + a seed hole ON it (point-on-curve
    COINCIDENT) + circular *sketch pattern*  -> the no-macro bolt-circle recipe
  - every sketch pre-checked with cad_sketch_analyze (offline, 0 API) before it is posted
  - all geometry driven by variables (#od/#length/#bore/#flat_x/#bcd/#mount)

Part (axis = Z; cadkit Top=world XY, Right=YZ normal +X — verified via evPlane):
  - body  : Ø#od cylinder, #length tall, grounded on the axis
  - D-bore: Ø#bore with a flat, across-flats #flat (flat plane at #flat_x from axis), REMOVE thru
            (flat located by a point-to-POINT horizontal dim — point-to-LINE is redundant with
             VERTICAL and regenerates WARNING)
  - mount : 4 × Ø#mount holes on a Ø#bcd circle, clocked 45° (so none sits on the +X set-screw
            axis), REMOVE thru
  - screw : Ø#setdia radial set screw on the Right plane, +X onto the flat, REMOVE thru the wall
Expect: 1 solid; bore is a D (not a full circle); 4 holes ride the #bcd circle; a radial
set-screw hole on the +X side reaching the bore.

Defaults: 6 mm D-shaft, 16 mm OD, 12 mm long, 10 mm bolt circle, 4 × M3-clearance, M3 set screw.

Budget: ~19 successful API calls. Fresh part studio in the existing test doc.
Run ONCE: on any non-OK status, read the per-step output — do not blind-retry.
"""
import asyncio
import json
import math

from cadkit_mcp import server as S

DOC = "550b17de256c3edec2066d48"
WS = "53187ebf4f6319f9815dc853"
MM = 1.0 / 25.4  # mm -> inches (the builders take inches; dims carry the mm expressions)

# --- design parameters (mm) ---
BORE, FLAT = 6.0, 5.5          # D-shaft: 6 mm nominal, 5.5 mm across the flat
OD, LENGTH = 16.0, 12.0
BCD, MOUNT, NBOLTS = 10.0, 3.2, 4
SETDIA = 3.0                   # M3 radial set screw onto the flat
FLAT_X = BORE / 2 - (BORE - FLAT)          # 2.5 mm: axis -> flat plane
CHORD_H = math.sqrt((BORE / 2) ** 2 - FLAT_X ** 2)  # half the chord length


async def call(tool, **a):
    r = await S.dispatch(tool, {"documentId": DOC, "workspaceId": WS, **a})
    txt = r[0].text
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return txt


async def main():
    ps = await call("cad_part_studio_create", name="high-speed 6mm D-shaft hub")
    elem = ps["elementId"]
    url = f"https://cad.onshape.com/documents/{DOC}/w/{WS}/e/{elem}"
    print("element :", url)

    for name, expr in [("od", f"{OD} mm"), ("length", f"{LENGTH} mm"), ("bore", f"{BORE} mm"),
                       ("flat_x", f"{FLAT_X} mm"), ("bcd", f"{BCD} mm"), ("mount", f"{MOUNT} mm"),
                       ("setdia", f"{SETDIA} mm")]:
        await call("cad_set_variable", elementId=elem, name=name, expression=expr)

    status = {}

    # ---- 1. body : grounded Ø#od cylinder ----------------------------------
    beg = await call("cad_sketch_begin", elementId=elem, plane="Top", name="body")
    sid = beg["sessionId"]
    body = await call("cad_sketch_circle", elementId=elem, sessionId=sid,
                      center=[0.0, 0.0], radius=OD / 2 * MM)
    await call("cad_sketch_constrain", elementId=elem, sessionId=sid,
               type="ground_origin", a=f"{body['entityId']}.center")
    await call("cad_sketch_dimension", elementId=elem, sessionId=sid, kind="diameter",
               entity=body["entityId"], value="#od")
    a1 = await call("cad_sketch_analyze", elementId=elem, sessionId=sid)
    close = await call("cad_sketch_close", elementId=elem, sessionId=sid)
    ext = await call("cad_extrude", elementId=elem, sketchFeatureId=close["sketchFeatureId"],
                     depth="#length", operation="NEW", name="body")
    status["body"] = (a1.get("fullyDefined"), close.get("status"), ext.get("status"))

    # ---- 2. D-bore : major arc + chord, flat located by a horizontal dim ----
    beg = await call("cad_sketch_begin", elementId=elem, plane="Top", name="D-bore")
    sid = beg["sessionId"]
    r0 = BORE / 2 * MM
    arc = await call("cad_sketch_arc", elementId=elem, sessionId=sid, center=[0.0, 0.0],
                     start=[FLAT_X * MM, CHORD_H * MM], end=[FLAT_X * MM, -CHORD_H * MM])  # CCW major arc
    chord = await call("cad_sketch_line", elementId=elem, sessionId=sid,
                       start=[FLAT_X * MM, -CHORD_H * MM], end=[FLAT_X * MM, CHORD_H * MM])
    aid, lid = arc["entityId"], chord["entityId"]
    await call("cad_sketch_constrain", elementId=elem, sessionId=sid, type="coincident",
               a=f"{aid}.start", b=f"{lid}.end")
    await call("cad_sketch_constrain", elementId=elem, sessionId=sid, type="coincident",
               a=f"{aid}.end", b=f"{lid}.start")
    await call("cad_sketch_constrain", elementId=elem, sessionId=sid,
               type="ground_origin", a=f"{aid}.center")
    await call("cad_sketch_constrain", elementId=elem, sessionId=sid, type="vertical", a=lid)
    await call("cad_sketch_dimension", elementId=elem, sessionId=sid, kind="radius",
               entity=aid, value="#bore/2")
    await call("cad_sketch_dimension", elementId=elem, sessionId=sid, kind="distance",
               entity=f"{aid}.center", entity2=f"{lid}.start", value="#flat_x", direction="horizontal")
    # NOTE: this D-bore sketch regenerates with a benign WARNING. Geometry is correct and the
    # sketch is fully defined (removing any constraint under-defines it, so there's no truly
    # redundant constraint) — it's an Onshape quirk for a symmetric center-arc + chord D-profile.
    # The textbook clean fix (mirror the chord across a construction X-axis with a SYMMETRIC
    # constraint) is currently blocked by a cadkit bug: SketchSession.symmetric() emits invalid
    # JSON (Onshape 400 BTWeirdStringValueException). Tracked as a follow-up; does not affect the part.
    a2 = await call("cad_sketch_analyze", elementId=elem, sessionId=sid)
    close = await call("cad_sketch_close", elementId=elem, sessionId=sid)
    ext = await call("cad_extrude", elementId=elem, sketchFeatureId=close["sketchFeatureId"],
                     depth="#length", operation="REMOVE", name="D-bore")
    status["bore"] = (a2.get("fullyDefined"), close.get("status"), ext.get("status"))

    # ---- 3. mounting bolt circle : point-on-curve seed + circular pattern ----
    beg = await call("cad_sketch_begin", elementId=elem, plane="Top", name="mount holes")
    sid = beg["sessionId"]
    con = await call("cad_sketch_circle", elementId=elem, sessionId=sid,
                     center=[0.0, 0.0], radius=BCD / 2 * MM, construction=True)
    await call("cad_sketch_constrain", elementId=elem, sessionId=sid,
               type="ground_origin", a=f"{con['entityId']}.center")
    await call("cad_sketch_dimension", elementId=elem, sessionId=sid, kind="diameter",
               entity=con["entityId"], value="#bcd")
    # Clock the seed to 45° (NOT on the X axis) so no hole lands on the set-screw axis (+X) —
    # at 0° the (bcd/2, 0) hole and the radial set screw collide. With 4 holes the ring then
    # sits at 45/135/225/315, clearing the screw.
    seed = await call("cad_sketch_circle", elementId=elem, sessionId=sid,
                      center=[BCD / 2 * math.cos(math.radians(45)) * MM,
                              BCD / 2 * math.sin(math.radians(45)) * MM], radius=MOUNT / 2 * MM)
    await call("cad_sketch_dimension", elementId=elem, sessionId=sid, kind="diameter",
               entity=seed["entityId"], value="#mount")
    await call("cad_sketch_constrain", elementId=elem, sessionId=sid, type="coincident",
               a=f"{seed['entityId']}.center", b=con["entityId"])         # seed ON the #bcd circle
    await call("cad_sketch_dimension", elementId=elem, sessionId=sid, kind="position",
               entity=f"{seed['entityId']}.center",
               value=[None, "#bcd/2 * sin(45 deg)"])  # on-circle + this y-height -> exactly 45°
    await call("cad_sketch_pattern", elementId=elem, sessionId=sid,
               entityIds=[seed["entityId"]], kind="circular", count=NBOLTS,
               center=[0.0, 0.0], angle=360.0 / NBOLTS)
    a3 = await call("cad_sketch_analyze", elementId=elem, sessionId=sid)
    close = await call("cad_sketch_close", elementId=elem, sessionId=sid)
    ext = await call("cad_extrude", elementId=elem, sketchFeatureId=close["sketchFeatureId"],
                     depth="#length", operation="REMOVE", name="mount holes")
    status["mount"] = (a3.get("fullyDefined"), close.get("status"), ext.get("status"))

    # ---- 4. set screw : radial M3 onto the flat (Right plane = YZ, extrude +X) ----
    beg = await call("cad_sketch_begin", elementId=elem, plane="Right", name="set screw")
    sid = beg["sessionId"]
    ss = await call("cad_sketch_circle", elementId=elem, sessionId=sid,
                    center=[0.0, LENGTH / 2 * MM], radius=SETDIA / 2 * MM)  # sketch(x,y)->world(Y,Z)
    await call("cad_sketch_dimension", elementId=elem, sessionId=sid, kind="diameter",
               entity=ss["entityId"], value="#setdia")
    await call("cad_sketch_dimension", elementId=elem, sessionId=sid, kind="position",
               entity=f"{ss['entityId']}.center", value=["0 mm", "#length/2"])  # on axis, mid-height
    a4 = await call("cad_sketch_analyze", elementId=elem, sessionId=sid)
    close = await call("cad_sketch_close", elementId=elem, sessionId=sid)
    ext = await call("cad_extrude", elementId=elem, sketchFeatureId=close["sketchFeatureId"],
                     depth="#od", operation="REMOVE", name="set screw")  # +X through the wall to the bore
    status["screw"] = (a4.get("fullyDefined"), close.get("status"), ext.get("status"))

    # ---- measure / verdict -------------------------------------------------
    m = await call("cad_measure", elementId=elem)

    print("\n=== STEP RESULTS  (fullyDefined, sketch, extrude) ===")
    for k, v in status.items():
        print(f"  {k:6}: {v}")
    print("measure:", json.dumps(m))

    ok = all(s[1] == "OK" and s[2] == "OK" for s in status.values())
    one_solid = m.get("solidCount") == 1
    print("\n=== VERDICT ===")
    print(f"all features OK : {ok}")
    print(f"single solid    : {one_solid}  (count={m.get('solidCount')})")
    print(f"element         : {url}")
    print("\nBUILD:", "PASS ✅" if (ok and one_solid) else "see steps above ❌")


if __name__ == "__main__":
    asyncio.run(main())
