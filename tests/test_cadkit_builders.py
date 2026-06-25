"""Offline builder tests for cadkit — assert on the JSON the builders EMIT.

These run with **zero API calls** (the expensive, quota-bounded behaviors live in the
on-demand live smoke test, not here). They guard the class of bug that actually cost a
debugging session: a wrong/hidden parameterId that produces plausible-but-dead output.
"""
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cadkit_mcp.sketch import SketchSession, ORIGIN_VERTEX  # noqa: E402
from cadkit_mcp import server as S  # noqa: E402  (imports a client but makes no network call)


def _session() -> SketchSession:
    return SketchSession("d", "w", "e", "Front", "t")


# ---- SketchSession (pure, no client) --------------------------------------
def test_ground_origin_uses_external_origin_vertex():
    s = _session(); l = s.add_line((0, 0), (2, 0)); s.ground_origin(f"{l}.start")
    con = s.constraints[-1]
    assert con["constraintType"] == "COINCIDENT"
    q = con["parameters"][0]
    assert q["btType"].startswith("BTMParameterQueryList")
    assert ORIGIN_VERTEX in q["queries"][0]["deterministicIds"]


def test_diagnostics_flags_ungrounded_and_undimensioned():
    s = _session(); s.add_line((0, 0), (2, 0))
    d = s.diagnostics()
    assert d["grounded"] is False and d["dimensions"] == 0 and d["wellFormed"] is False


def test_diagnostics_wellformed_when_grounded_and_dimensioned():
    s = _session(); l = s.add_line((0, 0), (2, 0))
    s.ground_origin(f"{l}.start"); s.dim_length(l, "#leg")
    d = s.diagnostics()
    assert d["grounded"] and d["dimensions"] == 1 and d["wellFormed"]


# ---- analyze() / auto-dimension (P4-3) ------------------------------------
def _grounded_dimensioned_rect(s, c1=(0, 0), c2=(2, 1)):
    """A rectangle that reaches 0-DOF: 4 lines + corners + H/V, grounded, width+height."""
    r = s.add_rectangle(c1, c2)
    s.ground_origin(f"{r['bottom']}.start")   # corner at c1 pinned to origin
    s.dim_length(r["bottom"], abs(c2[0] - c1[0]))
    s.dim_length(r["right"], abs(c2[1] - c1[1]))
    return r


def test_analyze_lone_circle_reports_three_dof_and_proposes_diameter():
    s = _session(); cid = s.add_circle((0, 0), 0.5)
    a = s.analyze()
    assert a["dof"] == 3 and a["grounded"] is False and a["fullyDefined"] is False
    sizes = [h for h in a["hints"] if h.get("entityId") == cid and h["category"] == "size"]
    assert sizes and sizes[0]["propose"] == "diameter"
    assert any(h.get("category") == "location" for h in a["hints"])
    assert a["applied"] == []  # report mode never mutates


def test_analyze_apply_locks_current_diameter_and_shrinks_dof():
    s = _session(); cid = s.add_circle((0, 0), 0.5)
    a = s.analyze(apply=True)
    assert a["applied"] == [{"entityId": cid, "dim": "diameter", "value": 1.0}]
    assert any(c["constraintType"] == "DIAMETER" for c in s.constraints)
    assert a["dof"] == 2  # diameter removed 1; center still free + ungrounded
    assert a["fullyDefined"] is False  # apply never grounds (placement is a design choice)


def test_analyze_ungrounded_rectangle_has_four_dof():
    s = _session(); s.add_rectangle((0, 0), (2, 1))
    a = s.analyze()
    assert a["dof"] == 4 and a["grounded"] is False


def test_analyze_grounded_dimensioned_rectangle_is_fully_defined():
    s = _session(); _grounded_dimensioned_rect(s)
    a = s.analyze()
    assert a["dof"] == 0 and a["fullyDefined"] is True
    assert not any(h.get("category") in ("size", "orientation") for h in a["hints"])


def test_analyze_apply_never_adds_line_lengths_or_overconstrains_a_rect():
    s = _session(); _grounded_dimensioned_rect(s)
    n_before = len(s.constraints)
    a = s.analyze(apply=True)  # lines already H/V, no circles -> nothing safe to add
    assert a["applied"] == [] and len(s.constraints) == n_before
    assert a["fullyDefined"] is True


def test_analyze_apply_orients_axis_aligned_line_but_not_a_diagonal():
    s = _session(); h = s.add_line((0, 0), (2, 0)); d = s.add_line((0, 0), (2, 1))
    a = s.analyze(apply=True)
    applied = {x["entityId"]: x["dim"] for x in a["applied"]}
    assert applied.get(h) == "horizontal"          # axis-aligned -> safe to auto-orient
    assert d not in applied                         # diagonal -> stays a hint, never guessed
    assert any(x.get("entityId") == d and x["category"] == "orientation" for x in a["hints"])


def test_native_hole_overrides_template_fields():
    locq = {"btType": "BTMIndividualQuery-138", "deterministicIds": ["II"]}
    j = S._hole_native_json(locq, ["JHD"], "countersink", "#d", 0.8, "csk",
                            up=True, csink_dia=0.55, csink_angle=82)["feature"]
    p = {x["parameterId"]: x for x in j["parameters"]}
    assert j["featureType"] == "hole"
    assert p["styleV2"]["value"] == "C_SINK" and p["style"]["value"] == "C_SINK"
    assert p["oppositeDirection"]["value"] is True
    assert p["holeDiameterV3"]["expression"] == "#d"          # passes #variables through
    assert p["cSinkDiameterV3"]["expression"] == "0.55 in"
    assert p["cSinkAngleV3"]["expression"] == "82 deg"
    assert p["locations"]["queries"] == [locq]
    assert p["scope"]["queries"][0]["deterministicIds"] == ["JHD"]


def test_selection_finders_strip_units_and_target_axis():
    # REGRESSION: FeatureScript throws on comparing a length/area (with units) to a plain number,
    # so the finders must divide out units before the comparison.
    from cadkit_mcp import selection as sl
    area = sl.fs_faces_by_area(True)
    assert "evArea" in area and "/ (inch * inch)" in area
    face_z = sl.fs_extreme_faces("Z", want_max=True)
    assert "minCorner[2]" in face_z and "/ inch" in face_z and ">" in face_z
    edge_x_min = sl.fs_extreme_edges("X", want_max=False)
    assert "minCorner[0]" in edge_x_min and "<" in edge_x_min  # min picks the lower extreme


def test_add_slot_emits_rect_plus_two_caps():
    s = _session()
    out = s.add_slot((0, 0), (2, 0), 0.6)
    assert len(out["sides"]) == 4 and len(out["caps"]) == 2     # 4 rect lines + a circle each end
    # the cap circles sit at the two centres, radius = width/2
    cap_centers = []
    for e in s.entities:
        if e["btType"] == "BTMSketchCurve-4":
            g = e["geometry"]
            cap_centers.append((round(g["xCenter"] / 0.0254, 3), round(g["radius"] / 0.0254, 3)))
    assert (0.0, 0.3) in cap_centers and (2.0, 0.3) in cap_centers


def test_add_point_emits_sketch_point_in_meters():
    s = _session()
    pid = s.add_point((1.0, 0.5))
    pt = [e for e in s.entities if e.get("entityId") == pid][0]
    assert pt["btType"].startswith("BTMSketchPoint")
    assert abs(pt["x"] - 1.0 * 0.0254) < 1e-9 and abs(pt["y"] - 0.5 * 0.0254) < 1e-9


def test_add_arc_emits_partial_ccw_segment_on_circle_geometry():
    import math
    s = _session()
    aid = s.add_arc((0, 0), (1, 0), (0, 1))           # quarter arc, CCW
    arc = [e for e in s.entities if e.get("entityId") == aid][0]
    assert arc["btType"].startswith("BTMSketchCurveSegment")
    assert arc["geometry"]["btType"].startswith("BTCurveGeometryCircle")
    assert arc["geometry"]["clockwise"] is False       # one proven parameterization, like add_circle
    assert abs(arc["geometry"]["radius"] - 1.0 * 0.0254) < 1e-9   # radius fixed by `start`, in metres
    sweep = arc["endParam"] - arc["startParam"]
    assert abs(sweep - math.pi / 2) < 1e-9             # quarter turn
    assert 0 < sweep < 2 * math.pi                     # partial, not a full circle


def test_add_arc_swap_endpoints_gives_complementary_major_arc():
    import math
    s = _session()
    aid = s.add_arc((0, 0), (0, 1), (1, 0))           # start/end swapped from the quarter arc
    arc = [e for e in s.entities if e.get("entityId") == aid][0]
    sweep = arc["endParam"] - arc["startParam"]
    assert abs(sweep - 3 * math.pi / 2) < 1e-9         # the major (270°) arc — CCW the other way around


def test_radius_dim_targets_circle_and_arc_entities_directly():
    # A circle is now a single closed curve, so its radius dim binds the circle entity itself
    # (no `.a` sub-arc); a standalone arc binds its own entity too.
    s = _session()
    cir = s.add_circle((0, 0), 0.5); s.dim_radius(cir, 0.5)
    cir_ref = [p["value"] for p in s.constraints[-1]["parameters"]
               if p["btType"].startswith("BTMParameterString")][0]
    assert cir_ref == cir
    arc = s.add_arc((2, 0), (3, 0), (2, 1)); s.dim_radius(arc, 1.0)
    arc_ref = [p["value"] for p in s.constraints[-1]["parameters"]
               if p["btType"].startswith("BTMParameterString")][0]
    assert arc_ref == arc


def test_add_fillet_trims_lines_inserts_tangent_arc_and_drops_corner_coincident():
    s = _session()
    h = s.add_line((0, 0), (2, 0))     # ln1 — horizontal from the corner
    v = s.add_line((0, 0), (0, 2))     # ln2 — vertical from the corner
    s.coincident(f"{h}.start", f"{v}.start")   # the corner join a polyline/rect would have
    out = s.add_fillet(h, v, 0.5)
    # geometry: 90° corner, r=0.5 -> tangent points 0.5 along each line, center at (0.5,0.5)
    assert abs(out["radius"] - 0.5) < 1e-12
    assert abs(out["center"][0] - 0.5) < 1e-9 and abs(out["center"][1] - 0.5) < 1e-9
    tps = sorted(out["tangentPoints"])
    assert abs(tps[0][0] - 0.0) < 1e-9 and abs(tps[0][1] - 0.5) < 1e-9
    assert abs(tps[1][0] - 0.5) < 1e-9 and abs(tps[1][1] - 0.0) < 1e-9
    # the arc exists, radius 0.5in, and sits on circle geometry
    arc = [e for e in s.entities if e.get("entityId") == out["arc"]][0]
    assert arc["geometry"]["btType"].startswith("BTCurveGeometryCircle")
    assert abs(arc["geometry"]["radius"] - 0.5 * 0.0254) < 1e-12
    # both lines trimmed back to length 1.5 (2.0 - 0.5 run)
    for ln in (h, v):
        e = [x for x in s.entities if x.get("entityId") == ln][0]
        assert abs(e["endParam"] / 0.0254 - 1.5) < 1e-9
    # the old corner coincident (ln1.start==ln2.start) is gone; replaced by coincidences to the arc
    corner = {f"{h}.start", f"{v}.start"}
    survivors = [c for c in s.constraints if c["constraintType"] == "COINCIDENT"
                 and {p.get("value") for p in c["parameters"] if p["btType"].startswith("BTMParameterString")} == corner]
    assert not survivors, "fillet must drop the corner coincident so the trimmed ends aren't forced together"
    assert sum(1 for c in s.constraints if c["constraintType"] == "TANGENT") == 2


def test_add_fillet_rejects_radius_too_large_to_fit():
    import pytest
    s = _session()
    h = s.add_line((0, 0), (1, 0)); v = s.add_line((0, 0), (0, 1))
    with pytest.raises(ValueError):
        s.add_fillet(h, v, 5.0)        # needs 5in of run on a 1in line


def test_adjacent_finder_is_structurally_a_qadjacent_face_sweep():
    # The qAdjacent signature is now LIVE-VERIFIED (smoke_fillet_adjacency.py: 5 side faces on a
    # box). This test pins the emitted shape so a refactor can't silently break the seed sweep.
    from cadkit_mcp import selection as sel
    fs = sel.fs_faces_adjacent_to_extreme("Z", True)
    assert "qAdjacent(best, AdjacencyType.EDGE, EntityType.FACE)" in fs
    assert "evBox3d" in fs and "transientQueriesToStrings" in fs


def _circle_center(s, cid):
    e = [x for x in s.entities if x.get("entityId") == cid and x["btType"] == "BTMSketchCurve-4"][0]
    g = e["geometry"]
    return g["xCenter"] / 0.0254, g["yCenter"] / 0.0254


def test_linear_pattern_copies_circle_along_direction():
    s = _session()
    c = s.add_circle((0, 0), 0.25)
    mapping = s.add_pattern([c], "linear", count=3, direction=[1, 0], spacing=1.0)
    copies = mapping[c]
    assert len(copies) == 2                       # count incl. original -> 2 copies
    cx1, cy1 = _circle_center(s, copies[0]); cx2, cy2 = _circle_center(s, copies[1])
    assert abs(cx1 - 1.0) < 1e-9 and abs(cy1) < 1e-9
    assert abs(cx2 - 2.0) < 1e-9 and abs(cy2) < 1e-9


def test_circular_pattern_rotates_circle_about_center():
    import math
    s = _session()
    c = s.add_circle((1, 0), 0.1)
    mapping = s.add_pattern([c], "circular", count=4, center=[0, 0], angle=90.0)
    pts = [_circle_center(s, cp) for cp in mapping[c]]
    assert len(pts) == 3
    for (x, y), (ex, ey) in zip(pts, [(0, 1), (-1, 0), (0, -1)]):   # 90/180/270 deg
        assert abs(x - ex) < 1e-9 and abs(y - ey) < 1e-9


def test_pattern_rejects_bad_count_and_unpatternable_entity():
    import pytest
    s = _session()
    c = s.add_circle((0, 0), 0.25)
    with pytest.raises(ValueError):
        s.add_pattern([c], "linear", count=1, direction=[1, 0], spacing=1.0)   # count < 2
    arc = s.add_arc((2, 0), (3, 0), (2, 1))
    with pytest.raises(ValueError):
        s.add_pattern([arc], "linear", count=2, direction=[1, 0], spacing=1.0)  # arcs not yet


def _pattern_con(s):
    return next(k for k in s.constraints if "PATTERN" in k["constraintType"])


def _pat_params(con):
    # quantity params carry both value(0.0) and expression("4"); prefer the expression
    out = {}
    for p in con["parameters"]:
        out[p["parameterId"]] = p["expression"] if "expression" in p else p.get("value")
    return out


def test_linked_circular_pattern_emits_constraint_roles_center0_curve1():
    s = _session()
    c = s.add_circle((1, 0), 0.1)
    out = s.add_linked_circle_pattern(c, "circular", count=4, center=[0, 0], angle=90.0)
    assert len(out["copies"]) == 3                       # count incl. seed -> 3 copies
    con = _pattern_con(s)
    assert con["constraintType"] == "CIRCULAR_PATTERN"
    p = _pat_params(con)
    assert p["patternc1"] == "4"                         # count
    # circular role ordering: curve at index 1, center at index 0 (ground-truth verified)
    assert p["localInstance1,0"] == c and p["localInstance0,0"] == f"{c}.center"
    assert p["localInstance1,1"] == out["copies"][0]
    assert p["localInstance0,1"] == f"{out['copies'][0]}.center"
    assert p["localPivot"].endswith(".center")           # the construction pivot point
    assert p["sketchToolType"] == "PATTERN"
    # copy ids follow the {ns}.0.{k}.0 scheme
    assert out["copies"][0].endswith(".0.1.0") and out["copies"][1].endswith(".0.2.0")


def test_linked_linear_pattern_emits_constraint_roles_curve0_center1():
    s = _session()
    c = s.add_circle((0, 0), 0.25)
    out = s.add_linked_circle_pattern(c, "linear", count=3, direction=[1, 0], spacing=1.0)
    assert len(out["copies"]) == 2
    con = _pattern_con(s)
    assert con["constraintType"] == "LINEAR_PATTERN"
    p = _pat_params(con)
    assert p["patternc1"] == "3" and p["patternc2"] == "1"
    # linear role ordering: curve at index 0, center at index 1 (REVERSED from circular)
    assert p["localInstance0,0,0"] == c and p["localInstance1,0,0"] == f"{c}.center"
    assert p["localInstance0,1,0"] == out["copies"][0]
    assert "localDirection1" in p                         # construction direction line
    assert p["sketchToolType"] == "PATTERN"


def test_linked_pattern_copies_are_single_curve_circles_at_right_coords():
    s = _session()
    c = s.add_circle((1, 0), 0.1)
    out = s.add_linked_circle_pattern(c, "circular", count=4, center=[0, 0], angle=90.0)
    # the copies are real BTMSketchCurve-4 circles at 90/180/270 deg about the origin
    pts = [_circle_center(s, cp) for cp in out["copies"]]
    for (x, y), (ex, ey) in zip(pts, [(0, 1), (-1, 0), (0, -1)]):
        assert abs(x - ex) < 1e-9 and abs(y - ey) < 1e-9


def test_linked_pattern_rejects_non_circle():
    import pytest
    s = _session()
    ln = s.add_line((0, 0), (1, 0))
    with pytest.raises(ValueError):
        s.add_linked_circle_pattern(ln, "linear", count=3, direction=[1, 0], spacing=1.0)


# MIRROR constraint live-verified in scripts/smoke_sketch_mirror.py (half-diamond -> 2.0in^3 rhombus).
def test_add_mirror_reflects_lines_across_axis_and_links_with_mirror_constraint():
    s = _session()
    axis = s.add_line((0, 0), (0, 1), construction=True)   # the Y axis as a construction line
    ln = s.add_line((1, 0), (1, 2))                         # a vertical line at x=1
    mapping = s.add_mirror([ln], axis)
    copy = mapping[ln]
    e = [x for x in s.entities if x.get("entityId") == copy][0]
    g = e["geometry"]
    # reflected across x=0 -> x=-1, same height/length
    assert abs(g["pntX"] / 0.0254 - (-1.0)) < 1e-9
    assert abs(g["pntY"] / 0.0254 - 0.0) < 1e-9
    assert abs(e["endParam"] / 0.0254 - 2.0) < 1e-9
    # a MIRROR constraint ties original -> copy about the axis line
    mir = [c for c in s.constraints if c["constraintType"] == "MIRROR"]
    assert len(mir) == 1
    vals = [p["value"] for p in mir[0]["parameters"] if p["btType"].startswith("BTMParameterString")]
    assert vals == [ln, copy, axis]


def test_add_mirror_rejects_non_line_entity():
    import pytest
    s = _session()
    axis = s.add_line((0, 0), (0, 1), construction=True)
    cir = s.add_circle((1, 1), 0.5)
    with pytest.raises(ValueError):
        s.add_mirror([cir], axis)       # lines only for now


def test_on_plane_finders_test_thinness_and_coordinate_on_the_right_axis():
    from cadkit_mcp import selection as sel
    f = sel.fs_faces_on_plane("Z", 0.0)
    assert "minCorner[2]" in f and "maxCorner[2]" in f       # Z axis index
    assert "abs(hi - lo)" in f and "abs((lo + hi)/2 - (0.0))" in f  # thin AND at the coord
    assert sel.SOLID_FACES in f
    e = sel.fs_edges_on_plane("X", 1.5)
    assert "minCorner[0]" in e and "(1.5)" in e
    assert sel.SOLID_EDGES in e


def test_circle_is_single_closed_curve_one_radius():
    # The circle is one native BTMSketchCurve-4 (no two semicircle arcs), so a single diameter/
    # radius dim drives the whole circle. Replaces the old 2-arc + EQUAL form whose split radius
    # caused the lopsided "teardrop" bore.
    s = _session()
    c = s.add_circle((1, 1), 0.5)
    curves = [e for e in s.entities if e.get("entityId") == c]
    assert len(curves) == 1 and curves[0]["btType"] == "BTMSketchCurve-4"
    assert abs(curves[0]["geometry"]["radius"] - 0.5 * 0.0254) < 1e-9
    # the old sub-arcs and the EQUAL hack must be gone
    assert not [e for e in s.entities if e.get("entityId") in (f"{c}.a", f"{c}.b")]
    assert not [k for k in s.constraints if k["constraintType"] == "EQUAL"]


def test_circle_references_resolve_and_center_is_groundable():
    # The only ids a circle exposes are the curve itself and its center point; both must resolve.
    # The old 2-arc form's missing-reference coincidents (cir.a.end / cir.b.start) caused WARNING.
    s = _session()
    c = s.add_circle((0, 0), 0.5)
    s.dim_diameter(c, 1.0); s.ground_origin(f"{c}.center")
    valid = {c, f"{c}.center"}
    refs = {p["value"] for con in s.constraints
            for p in con["parameters"] if p["btType"].startswith("BTMParameterString")}
    bad = {r for r in refs if r not in valid}
    assert not bad, f"circle constraints reference non-existent ids: {bad}"
    # the center is exposed via the curve's centerId (NOT a separate point entity — that redundant
    # point regenerates WARNING); `{c}.center` is still groundable/referenceable.
    curve = next((e for e in s.entities if e.get("entityId") == c), None)
    assert curve is not None and curve.get("centerId") == f"{c}.center"
    assert not [e for e in s.entities if e.get("entityId") == f"{c}.center"]


def test_dim_length_accepts_variable_expression():
    s = _session(); l = s.add_line((0, 0), (2, 0)); s.dim_length(l, "#leg_len")
    q = [p for p in s.constraints[-1]["parameters"] if p.get("parameterId") == "length"][0]
    assert q["expression"] == "#leg_len"


# ---- directional distance + variable-driven center placement (P4-2) -------
def _dir_enum(con):
    return next((p for p in con["parameters"] if p.get("parameterId") == "direction"), None)


def test_dim_distance_plain_has_no_direction_enum():
    s = _session(); a = s.add_point((0, 0)); b = s.add_point((2, 0))
    s.dim_distance(a, b, 2.0)
    con = s.constraints[-1]
    assert con["constraintType"] == "DISTANCE" and _dir_enum(con) is None


def test_dim_distance_horizontal_emits_dimension_direction_enum():
    s = _session(); a = s.add_point((0, 0)); b = s.add_point((2, 1))
    s.dim_distance(a, b, "#dx", direction="horizontal")
    d = _dir_enum(s.constraints[-1])
    assert d["enumName"] == "DimensionDirection" and d["value"] == "HORIZONTAL"
    q = [p for p in s.constraints[-1]["parameters"] if p.get("parameterId") == "length"][0]
    assert q["expression"] == "#dx"


def test_dim_distance_rejects_unknown_direction():
    s = _session(); a = s.add_point((0, 0)); b = s.add_point((2, 0))
    with pytest.raises(ValueError):
        s.dim_distance(a, b, 1.0, direction="diagonal")


def test_dim_position_pins_point_to_origin_on_both_axes_with_variables():
    s = _session(); p = s.add_point((2, 3))
    s.dim_position(p, x="#hx", y="#hy")
    cons = s.constraints[-2:]
    dirs = {_dir_enum(c)["value"] for c in cons}
    assert dirs == {"HORIZONTAL", "VERTICAL"}
    for c in cons:
        # measured FROM the part-studio origin vertex (external) TO the local point
        q0 = c["parameters"][0]
        assert q0["btType"].startswith("BTMParameterQueryList")
        assert ORIGIN_VERTEX in q0["queries"][0]["deterministicIds"]
        assert c["parameters"][1]["value"] == p
    exprs = {[p2 for p2 in c["parameters"] if p2.get("parameterId") == "length"][0]["expression"]
             for c in cons}
    assert exprs == {"#hx", "#hy"}


def test_dim_position_single_axis_emits_one_constraint_and_counts_as_dimension():
    s = _session(); p = s.add_point((2, 0))
    n = len(s.constraints)
    s.dim_position(p, x=2.0)               # y omitted -> only the horizontal pin
    assert len(s.constraints) == n + 1
    assert _dir_enum(s.constraints[-1])["value"] == "HORIZONTAL"
    assert s.diagnostics()["dimensions"] == 1   # DISTANCE is a driving dimension


def test_dim_position_requires_at_least_one_axis():
    s = _session(); p = s.add_point((0, 0))
    with pytest.raises(ValueError):
        s.dim_position(p)


def test_dispatch_position_dimension_places_a_circle_center_by_variables():
    import asyncio
    async def go():
        b = await S.dispatch("cad_sketch_begin",
                             {"documentId": "d", "workspaceId": "w", "elementId": "e", "plane": "Top"})
        sid = json.loads(b[0].text)["sessionId"]
        cir = json.loads((await S.dispatch("cad_sketch_circle",
                          {"sessionId": sid, "center": [1, 1], "radius": 0.25}))[0].text)["entityId"]
        await S.dispatch("cad_sketch_dimension",
                         {"sessionId": sid, "kind": "diameter", "entity": cir, "value": 0.5})
        out = await S.dispatch("cad_sketch_dimension",
                         {"sessionId": sid, "kind": "position", "entity": f"{cir}.center",
                          "value": ["#hx", "#hy"]})
        assert out[0].text == "ok"
        s = S.SESSIONS[sid]
        pos = [c for c in s.constraints if c["entityId"].startswith("dpos")]
        assert {_dir_enum(c)["value"] for c in pos} == {"HORIZONTAL", "VERTICAL"}
    asyncio.run(go())


# ---- server JSON builders (pure functions) --------------------------------
def test_assign_variable_uses_anyValue_not_hidden_value():
    # REGRESSION: the original bug emitted parameterId "value" — an AlwaysHidden/legacy field
    # that silently fails to evaluate. The value must live in anyValue with variableType ANY.
    feat = S._assign_variable_json("w", "2 in")["feature"]
    pids = {p["parameterId"] for p in feat["parameters"]}
    assert "anyValue" in pids and "value" not in pids
    vt = [p for p in feat["parameters"] if p["parameterId"] == "variableType"][0]
    assert vt["value"] == "ANY"
    assert "featureId" not in feat  # create form omits featureId


def test_assign_variable_update_embeds_featureId():
    feat = S._assign_variable_json("w", "2 in", "FID123")["feature"]
    assert feat["featureId"] == "FID123"  # update form must carry the id


def test_scalar_expr_number_and_passthrough():
    assert S._scalar_expr(1.5) == "1.5 in"
    assert S._scalar_expr("#width") == "#width"


def test_extrude_depth_accepts_expression():
    j = S._extrude_json("FSK", "#width", "NEW", "x")["feature"]
    depth = [p for p in j["parameters"] if p["parameterId"] == "depth"][0]
    assert depth["expression"] == "#width"


def test_fillet_radius_expression_and_edges():
    j = S._fillet_json(["JHN"], "#r", "f")["feature"]
    rad = [p for p in j["parameters"] if p["parameterId"] == "radius"][0]
    assert rad["expression"] == "#r"
    ents = [p for p in j["parameters"] if p["parameterId"] == "entities"][0]
    assert ents["queries"][0]["deterministicIds"] == ["JHN"]


# ---- P1 feature builders --------------------------------------------------
def _ptypes(feature):
    return [p["parameterId"] for p in feature["parameters"]]


def test_chamfer_equal_offset_with_expression():
    j = S._chamfer_json(["E1"], "#c", "c")["feature"]
    assert j["featureType"] == "chamfer"
    ct = [p for p in j["parameters"] if p["parameterId"] == "chamferType"][0]
    assert ct["value"] == "EQUAL_OFFSETS"
    w = [p for p in j["parameters"] if p["parameterId"] == "width"][0]
    assert w["expression"] == "#c"


def test_revolve_full_vs_angle():
    full = S._revolve_json("F", "E2", None, "NEW", "r")["feature"]
    assert "fullRevolve" in _ptypes(full) and "angle" not in _ptypes(full)
    part = S._revolve_json("F", "E2", 90, "NEW", "r")["feature"]
    ang = [p for p in part["parameters"] if p["parameterId"] == "angle"][0]
    assert ang["expression"] == "90 deg"


def test_shell_thickness_inward():
    j = S._shell_json(["F1"], "#t", "s")["feature"]
    assert j["featureType"] == "shell"
    t = [p for p in j["parameters"] if p["parameterId"] == "thickness"][0]
    assert t["expression"] == "#t"


def test_sketch_on_face_targets_face_id():
    # plane that isn't a standard name is treated as a deterministic face id
    from cadkit_mcp.sketch import SketchSession
    s = SketchSession("d", "w", "e", "JABC123", "onface")
    plane_q = s.build()["feature"]["parameters"][0]
    assert plane_q["queries"][0]["deterministicIds"] == ["JABC123"]


def test_offset_plane_emits_cplane_offset():
    j = S._plane_json("JDC", 2.0, "p")["feature"]
    assert j["featureType"] == "cPlane"
    p = _params(j)
    assert p["cplaneType"]["value"] == "OFFSET"
    assert p["cplaneType"]["enumName"] == "CPlaneType"
    assert p["offset"]["expression"] == "2.0 in"          # number -> inches
    assert p["entities"]["queries"][0]["deterministicIds"] == ["JDC"]
    assert p["oppositeDirection"]["value"] is False        # flip defaults off


def test_offset_plane_offset_accepts_variable_and_flip():
    j = S._plane_json("JABC", "#gap", "p", flip=True)["feature"]
    p = _params(j)
    assert p["offset"]["expression"] == "#gap"             # #variable passes through
    assert p["oppositeDirection"]["value"] is True


def test_plane_of_feature_query_targets_created_bodies():
    from cadkit_mcp import selection as sel
    fs = sel.fs_plane_of_feature("FID123")
    assert 'qCreatedBy(makeId("FID123"), EntityType.FACE)' in fs
    assert "transientQueriesToStrings" in fs


# pattern/mirror are FEATURE-based: they repeat whole features (instanceFunction), not faces.
# REGRESSION: the original face-based form (patternType=FACE + a `faces` query) errored on
# regenerate. Assert the verified structure instead.
def _params(feature):
    return {p["parameterId"]: p for p in feature["parameters"]}


def test_linear_pattern_is_feature_based():
    lin = S._linear_pattern_json(["FEXT1"], "E1", "#d", 4, "p")["feature"]
    assert lin["featureType"] == "linearPattern"
    p = _params(lin)
    assert p["patternType"]["value"] == "FEATURE" and p["patternType"]["enumName"] == "PatternType"
    assert "faces" not in p  # the old (broken) face form must not reappear
    fl = p["instanceFunction"]
    assert fl["btType"].startswith("BTMParameterFeatureList") and fl["featureIds"] == ["FEXT1"]
    assert p["directionOne"]["queries"][0]["deterministicIds"] == ["E1"]
    assert p["distance"]["expression"] == "#d"
    assert p["instanceCount"]["expression"] == "4" and p["instanceCount"]["isInteger"] is True
    assert p["operationType"]["value"] == "NEW"  # additive default


def test_circular_pattern_is_feature_based():
    cir = S._circular_pattern_json(["FEXT1"], "JNB", 6, 360, "c")["feature"]
    p = _params(cir)
    assert cir["featureType"] == "circularPattern"
    assert p["patternType"]["value"] == "FEATURE"
    assert p["instanceFunction"]["featureIds"] == ["FEXT1"]
    assert p["axis"]["queries"][0]["deterministicIds"] == ["JNB"]
    assert p["angle"]["expression"] == "360 deg" and p["equalSpace"]["value"] is True
    assert p["operationType"]["value"] == "NEW"


def test_mirror_is_feature_based():
    mir = S._mirror_json(["FEXT1"], "JEC", "m")["feature"]
    p = _params(mir)
    assert mir["featureType"] == "mirror"
    assert p["patternType"]["value"] == "FEATURE" and p["patternType"]["enumName"] == "MirrorType"
    assert "faces" not in p
    assert p["instanceFunction"]["featureIds"] == ["FEXT1"]
    assert p["mirrorPlane"]["queries"][0]["deterministicIds"] == ["JEC"]
    assert p["operationType"]["value"] == "NEW"


def test_pattern_mirror_of_a_cut_use_remove_operation():
    # Patterning/mirroring a hole must REMOVE at each copy — NEW leaks a stray body for a cut.
    lin = S._linear_pattern_json(["HOLE1"], "E1", "#d", 3, "p", "REMOVE")["feature"]
    cir = S._circular_pattern_json(["HOLE1"], "JNB", 4, 360, "c", "REMOVE")["feature"]
    mir = S._mirror_json(["HOLE1"], "JEC", "m", "REMOVE")["feature"]
    for f in (lin, cir, mir):
        op = _params(f)["operationType"]
        assert op["value"] == "REMOVE" and op["enumName"] == "NewBodyOperationType"


# ---- P2 pure helpers ------------------------------------------------------
def test_scan_variables_reads_name_and_expression():
    # round-trip: the assignVariable JSON cad_set_variable emits must read back cleanly
    feat = S._assign_variable_json("leg", "#base*2", "FV1")["feature"]  # featureType set by builder
    others = [{"featureType": "extrude", "parameters": []}]
    vs = S._scan_variables([feat] + others)
    assert vs == [{"name": "leg", "expression": "#base*2", "featureId": "FV1"}]


def test_apply_param_edit_retargets_expression():
    j = S._extrude_json("FSK", "0.5 in", "NEW", "x")["feature"]
    S._apply_param_edit(j, "depth", expression="#width")
    depth = [p for p in j["parameters"] if p["parameterId"] == "depth"][0]
    assert depth["expression"] == "#width"


def test_apply_param_edit_sets_value_and_raises_on_missing():
    j = S._revolve_json("F", "E2", 90, "NEW", "r")["feature"]
    S._apply_param_edit(j, "operationType", value="REMOVE")
    op = [p for p in j["parameters"] if p["parameterId"] == "operationType"][0]
    assert op["value"] == "REMOVE"
    try:
        S._apply_param_edit(j, "nope", expression="1 in")
        assert False, "expected KeyError on missing parameter"
    except KeyError:
        pass


def test_measure_summary_shapes_bbox_and_size():
    parsed = {"solidCount": 2, "solidVolume": 1.25,
              "solidMin": [0, 0, 0], "solidMax": [2, 1, 0.5]}
    out = S._measure_summary(parsed)
    assert out["solidCount"] == 2 and out["volume"] == 1.25
    assert out["bbox"]["size"] == [2, 1, 0.5]


def test_measure_summary_handles_empty_studio():
    out = S._measure_summary({"solidCount": 0})
    assert out["solidCount"] == 0 and "bbox" not in out
