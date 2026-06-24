"""Offline builder tests for cadkit — assert on the JSON the builders EMIT.

These run with **zero API calls** (the expensive, quota-bounded behaviors live in the
on-demand live smoke test, not here). They guard the class of bug that actually cost a
debugging session: a wrong/hidden parameterId that produces plausible-but-dead output.
"""
import pathlib
import sys

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
        if e["btType"].startswith("BTMSketchCurveSegment") and e["entityId"].endswith(".a"):
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


def test_radius_dim_targets_arc_entity_directly_but_circle_sub_arc():
    # A circle's radius dim binds its `.a` sub-arc; a standalone arc has no `.a`, so the dim must
    # bind the arc entity itself — otherwise it emits a non-existent `arc1.a` and never drives.
    s = _session()
    cir = s.add_circle((0, 0), 0.5); s.dim_radius(cir, 0.5)
    cir_ref = [p["value"] for p in s.constraints[-1]["parameters"]
               if p["btType"].startswith("BTMParameterString")][0]
    assert cir_ref == f"{cir}.a"
    arc = s.add_arc((2, 0), (3, 0), (2, 1)); s.dim_radius(arc, 1.0)
    arc_ref = [p["value"] for p in s.constraints[-1]["parameters"]
               if p["btType"].startswith("BTMParameterString")][0]
    assert arc_ref == arc          # the entity itself, not arc.a


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


def test_circle_arcs_tied_equal_so_one_dim_drives_whole_circle():
    # REGRESSION: a circle is two semicircle arcs; without an EQUAL tying them, a single
    # diameter/radius dim binds only the .a arc and the .b arc floats to the placeholder ->
    # lopsided "teardrop"/chamfered bore (and an oversized loose arc can split a thin wall).
    s = _session()
    c = s.add_circle((1, 1), 0.5)
    eqs = [k for k in s.constraints if k["constraintType"] == "EQUAL"]
    tied = [k for k in eqs
            if {p.get("value") for p in k["parameters"] if p["btType"].startswith("BTMParameterString")}
            == {f"{c}.a", f"{c}.b"}]
    assert tied, "add_circle must EQUAL-tie its two arcs so one dimension drives the full circle"


def test_dim_length_accepts_variable_expression():
    s = _session(); l = s.add_line((0, 0), (2, 0)); s.dim_length(l, "#leg_len")
    q = [p for p in s.constraints[-1]["parameters"] if p.get("parameterId") == "length"][0]
    assert q["expression"] == "#leg_len"


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


def test_circular_pattern_is_feature_based():
    cir = S._circular_pattern_json(["FEXT1"], "JNB", 6, 360, "c")["feature"]
    p = _params(cir)
    assert cir["featureType"] == "circularPattern"
    assert p["patternType"]["value"] == "FEATURE"
    assert p["instanceFunction"]["featureIds"] == ["FEXT1"]
    assert p["axis"]["queries"][0]["deterministicIds"] == ["JNB"]
    assert p["angle"]["expression"] == "360 deg" and p["equalSpace"]["value"] is True


def test_mirror_is_feature_based():
    mir = S._mirror_json(["FEXT1"], "JEC", "m")["feature"]
    p = _params(mir)
    assert mir["featureType"] == "mirror"
    assert p["patternType"]["value"] == "FEATURE" and p["patternType"]["enumName"] == "MirrorType"
    assert "faces" not in p
    assert p["instanceFunction"]["featureIds"] == ["FEXT1"]
    assert p["mirrorPlane"]["queries"][0]["deterministicIds"] == ["JEC"]


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
