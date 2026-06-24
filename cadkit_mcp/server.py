"""cadkit — an Onshape MCP server that models the *human* way.

Single constrained sketches (origin-grounded, dimension-driven, fully defined),
variable-driven parametrics, and semantic geometry selection.
Reuses the onshape_mcp client/transport (installed in the same venv).
"""
import os, sys, json, asyncio, math, pathlib
from typing import Any, Dict, List, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from onshape_mcp.api.client import OnshapeClient, OnshapeCredentials
from onshape_mcp.api.partstudio import PartStudioManager
from onshape_mcp.api.featurescript import FeatureScriptManager
from onshape_mcp.api.documents import DocumentManager
from onshape_mcp.api.export import ExportManager

from .sketch import SketchSession, PLANES
from .devkit import measure_fs, parse_fs
from . import selection as sel
from . import quota

def _load_creds() -> OnshapeCredentials:
    ak, sk = os.getenv("ONSHAPE_ACCESS_KEY", ""), os.getenv("ONSHAPE_SECRET_KEY", "")
    if not (ak and sk):  # fall back to the existing onshape MCP config — keys never re-pasted
        try:
            cfg = json.loads(pathlib.Path("~/.claude.json").expanduser().read_text())
            def find(o):
                if isinstance(o, dict):
                    if "onshape" in o.get("mcpServers", {}):
                        return o["mcpServers"]["onshape"].get("env", {})
                    for v in o.values():
                        r = find(v)
                        if r: return r
            env = find(cfg) or {}
            ak = ak or env.get("ONSHAPE_ACCESS_KEY", ""); sk = sk or env.get("ONSHAPE_SECRET_KEY", "")
        except Exception:
            pass
    return OnshapeCredentials(access_key=ak, secret_key=sk)

client = OnshapeClient(_load_creds())
quota.instrument(client)        # count every successful API call so spend is visible, not a guess
PS = PartStudioManager(client)
FS = FeatureScriptManager(client)
DOCS = DocumentManager(client)
EXPORT = ExportManager(client)

SESSIONS: Dict[str, SketchSession] = {}
# (doc, ws, elem) -> {variable name: featureId}. Lets cad_set_variable update an existing
# Variable feature in place instead of appending a duplicate. Populated lazily with one
# get_features read per element, then kept warm so repeated sets cost only the write.
_VAR_CACHE: Dict[tuple, Dict[str, str]] = {}


async def _set_variable(doc: str, ws: str, elem: str, name: str, expression: str) -> Dict[str, Any]:
    key = (doc, ws, elem)
    if key not in _VAR_CACHE:
        existing: Dict[str, str] = {}
        feats = await PS.get_features(doc, ws, elem)
        for f in feats.get("features", []):
            if f.get("featureType") == "assignVariable":
                vn = next((p.get("value") for p in f.get("parameters", [])
                           if p.get("parameterId") == "name"), None)
                if vn:
                    existing[vn] = f.get("featureId")
        _VAR_CACHE[key] = existing
    cache = _VAR_CACHE[key]
    if name in cache:
        fid = cache[name]
        r = await PS.update_feature(doc, ws, elem, fid, _assign_variable_json(name, expression, fid))
        action = "updated"
    else:
        r = await PS.add_feature(doc, ws, elem, _assign_variable_json(name, expression))
        fid = r["feature"]["featureId"]
        cache[name] = fid
        action = "created"
    return {"status": r.get("featureState", {}).get("featureStatus"), "action": action, "featureId": fid}
_counter = {"n": 0}

def _new_session_id() -> str:
    _counter["n"] += 1
    return f"sk{_counter['n']}"

def _txt(s: str) -> List[TextContent]:
    return [TextContent(type="text", text=s)]

def _feat_result(r: Dict[str, Any], **extra) -> List[TextContent]:
    # every feature tool returns its new featureId so it can be fed to pattern/mirror/fillet/etc.
    return _txt(json.dumps({"status": r.get("featureState", {}).get("featureStatus"),
                            "featureId": r.get("feature", {}).get("featureId"), **extra}))


# ---- P2 pure helpers (offline-testable; no API) ---------------------------
def _scan_variables(features: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Pull cadkit-authored variables out of a feature list: name + authored expression.

    cad_set_variable stores each variable as an `assignVariable` feature — the name in the
    `name` string param, the expression in the `anyValue` quantity param. Reading the
    expression back is exact and unit-faithful (no metre/inch conversion guesswork)."""
    out = []
    for f in features:
        if f.get("featureType") != "assignVariable":
            continue
        params = f.get("parameters", [])
        name = next((p.get("value") for p in params if p.get("parameterId") == "name"), None)
        expr = next((p.get("expression") for p in params if p.get("parameterId") == "anyValue"), None)
        if name:
            out.append({"name": name, "expression": expr, "featureId": f.get("featureId")})
    return out


def _apply_param_edit(feature: Dict[str, Any], parameter_id: str,
                      expression: Optional[str] = None, value: Optional[Any] = None) -> Dict[str, Any]:
    """Mutate one stored parameter in a fetched feature, in place, and return it.

    `expression` retargets a quantity param (a dimension, a depth — '#var' or '0.5 in');
    `value` sets an enum/bool/string param. Raises if the parameter isn't present so the
    caller fails loudly instead of silently no-op'ing."""
    for p in feature.get("parameters", []):
        if p.get("parameterId") == parameter_id:
            if expression is not None:
                p["expression"] = expression
            if value is not None:
                p["value"] = value
            return feature
    raise KeyError(f"parameter '{parameter_id}' not found on feature "
                   f"'{feature.get('name', feature.get('featureId'))}'")


def _measure_summary(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a measure_fs eval result into {count, volume, bbox{min,max,size}} (inches)."""
    count = parsed.get("solidCount")
    out: Dict[str, Any] = {"solidCount": count}
    if parsed.get("solidMin") and parsed.get("solidMax"):
        lo, hi = parsed["solidMin"], parsed["solidMax"]
        out["volume"] = parsed.get("solidVolume")
        out["bbox"] = {"min": lo, "max": hi, "size": [hi[i] - lo[i] for i in range(3)]}
    return out

def _scalar_expr(value, unit: str = "in") -> str:
    """A scalar parameter as an Onshape expression: a number (in `unit`) or a raw
    expression / #variable passed through (e.g. 1.5 -> '1.5 in'; '#width' -> '#width')."""
    if isinstance(value, (int, float)):
        return f"{value} {unit}"
    return str(value)


def _extrude_json(sketch_fid: str, depth, op: str, name: str) -> Dict[str, Any]:
    return {"btType": "BTFeatureDefinitionCall-1406", "feature": {
        "btType": "BTMFeature-134", "featureType": "extrude", "name": name,
        "suppressed": False, "namespace": "", "parameters": [
            {"btType": "BTMParameterQueryList-148", "parameterId": "entities",
             "queries": [{"btType": "BTMIndividualSketchRegionQuery-140", "queryStatement": None,
                          "filterInnerLoops": True,
                          "queryString": f'query = qSketchRegion(id + "{sketch_fid}", true);',
                          "featureId": sketch_fid, "deterministicIds": []}]},
            {"btType": "BTMParameterEnum-145", "enumName": "NewBodyOperationType",
             "value": op, "parameterId": "operationType"},
            {"btType": "BTMParameterEnum-145", "enumName": "BoundingType",
             "value": "BLIND", "parameterId": "endBound"},
            {"btType": "BTMParameterQuantity-147", "expression": _scalar_expr(depth),
             "parameterId": "depth", "isInteger": False}]}}

def _assign_variable_json(name: str, expression: str, feature_id: Optional[str] = None) -> Dict[str, Any]:
    # The assignVariable ("Variable") feature stores its value in a TYPE-SPECIFIC
    # parameter — anyValue/lengthValue/angleValue/numberValue — gated by variableType.
    # The plain "value" parameter is AlwaysHidden/legacy and silently fails to evaluate
    # ("Cannot evaluate the variable", resolves to 0). We use variableType=ANY +
    # anyValue, which accepts any expression ("2 in", "0.25 in", "#other*2", numbers).
    # Verified OK against the live API featurespecs for the Variable feature.
    feature = {"btType": "BTMFeature-134", "featureType": "assignVariable",
        "name": name, "suppressed": False, "namespace": "",
        "parameters": [
            {"btType": "BTMParameterEnum-145", "enumName": "VariableType",
             "value": "ANY", "parameterId": "variableType"},
            {"btType": "BTMParameterString-149", "value": name, "parameterId": "name"},
            {"btType": "BTMParameterQuantity-147", "isInteger": False,
             "expression": expression, "parameterId": "anyValue"}]}
    if feature_id is not None:
        feature["featureId"] = feature_id          # required when updating in place
    return {"feature": feature}


def _fillet_json(edge_ids, radius, name: str) -> Dict[str, Any]:
    """Constant-radius fillet whose radius accepts a number or an expression/#variable."""
    return {"feature": {"btType": "BTMFeature-134", "featureType": "fillet",
        "name": name, "suppressed": False, "namespace": "", "parameters": [
            {"btType": "BTMParameterQueryList-148", "parameterId": "entities",
             "queries": [{"btType": "BTMIndividualQuery-138", "deterministicIds": list(edge_ids)}]},
            {"btType": "BTMParameterQuantity-147", "isInteger": False,
             "expression": _scalar_expr(radius), "parameterId": "radius"}]}}


# ---- generic parameter / feature constructors -----------------------------
def _qlist(pid: str, ids) -> Dict[str, Any]:
    return {"btType": "BTMParameterQueryList-148", "parameterId": pid,
            "queries": [{"btType": "BTMIndividualQuery-138", "deterministicIds": list(ids)}]}

def _enum(pid: str, enum_name: str, val: str) -> Dict[str, Any]:
    return {"btType": "BTMParameterEnum-145", "enumName": enum_name, "value": val, "parameterId": pid}

def _qty(pid: str, expr: str, integer: bool = False) -> Dict[str, Any]:
    return {"btType": "BTMParameterQuantity-147", "isInteger": integer, "expression": expr, "parameterId": pid}

def _flag(pid: str, val: bool) -> Dict[str, Any]:
    return {"btType": "BTMParameterBoolean-144", "value": val, "parameterId": pid}

def _featlist(pid: str, feature_ids) -> Dict[str, Any]:
    # references whole features (the thing a pattern/mirror repeats), not transient geometry
    return {"btType": "BTMParameterFeatureList-1749", "parameterId": pid, "featureIds": list(feature_ids)}

def _count_expr(count) -> str:
    return count if isinstance(count, str) else str(int(count))

def _region_param(sketch_fid: str) -> Dict[str, Any]:
    return {"btType": "BTMParameterQueryList-148", "parameterId": "entities",
            "queries": [{"btType": "BTMIndividualSketchRegionQuery-140", "filterInnerLoops": True,
                         "queryString": f'query = qSketchRegion(id + "{sketch_fid}", true);',
                         "featureId": sketch_fid, "deterministicIds": []}]}

def _feat(ftype: str, name: str, params) -> Dict[str, Any]:
    return {"feature": {"btType": "BTMFeature-134", "featureType": ftype, "name": name,
                        "suppressed": False, "namespace": "", "parameters": params}}


def _chamfer_json(edge_ids, distance, name: str) -> Dict[str, Any]:
    return _feat("chamfer", name, [
        _qlist("entities", edge_ids),
        _enum("chamferType", "ChamferType", "EQUAL_OFFSETS"),
        _qty("width", _scalar_expr(distance))])

def _revolve_json(sketch_fid: str, axis_id: str, angle, op: str, name: str) -> Dict[str, Any]:
    params = [_enum("bodyType", "ExtendedToolBodyType", "SOLID"),
              _enum("operationType", "NewBodyOperationType", op),
              _region_param(sketch_fid), _qlist("axis", [axis_id])]
    if angle is None:
        params.append(_flag("fullRevolve", True))
    else:
        params += [_flag("fullRevolve", False),
                   _enum("endBound", "RevolveBoundingType", "BLIND"),
                   _qty("angle", _scalar_expr(angle, "deg"))]
    return _feat("revolve", name, params)

def _shell_json(face_ids, thickness, name: str) -> Dict[str, Any]:
    return _feat("shell", name, [
        _qlist("entities", face_ids),
        _qty("thickness", _scalar_expr(thickness)),
        _flag("oppositeDirection", True)])  # shell inward (keep outer dimensions)

_HOLE_TEMPLATE = json.loads((pathlib.Path(__file__).parent / "hole_template.json").read_text())
_HOLE_STYLE = {"simple": "SIMPLE", "counterbore": "C_BORE", "countersink": "C_SINK"}

def _hole_native_json(locations_query, scope_ids, style: str, diameter, depth, name: str,
                      up: bool = False, through: bool = False, cbore_dia=None, cbore_depth=None,
                      csink_dia=None, csink_angle=90) -> Dict[str, Any]:
    """The native Onshape `hole` feature (V3) — proper hole with callouts, counterbore/countersink.

    Built from the full known-good 160-param template (a trimmed/guessed set regenerates to ERROR);
    we override only the meaningful fields. `locations_query` is a sketch-point query; `up` flips
    the drill direction (oppositeDirection) — set it so the hole drills INTO the part. V3 fields
    and their legacy mirrors are both set so the version-matching logic stays consistent."""
    feat = json.loads(json.dumps(_HOLE_TEMPLATE))   # deep copy
    feat["name"] = name
    se = _HOLE_STYLE[style]
    end = "THROUGH" if through else "BLIND"
    by_id = {p.get("parameterId"): p for p in feat["parameters"]}

    def setv(pid, value):
        if pid in by_id: by_id[pid]["value"] = value

    def setx(pid, expr):
        if pid in by_id: by_id[pid]["expression"] = expr

    setv("styleV2", se); setv("style", se)
    setv("endStyleV2", end); setv("endStyle", end)
    setv("oppositeDirection", bool(up))
    by_id["locations"]["queries"] = [locations_query]
    by_id["scope"]["queries"] = [{"btType": "BTMIndividualQuery-138", "deterministicIds": list(scope_ids)}]
    for pid in ("holeDiameterV3", "holeDiameterV2", "holeDiameter"):
        setx(pid, _scalar_expr(diameter))
    if not through:
        for pid in ("holeDepthV3", "holeDepth"):
            setx(pid, _scalar_expr(depth))
    if style == "counterbore":
        for pid in ("cBoreDiameterV3", "cBoreDiameter"): setx(pid, _scalar_expr(cbore_dia))
        for pid in ("cBoreDepthV3", "cBoreDepth"): setx(pid, _scalar_expr(cbore_depth))
    elif style == "countersink":
        for pid in ("cSinkDiameterV3", "cSinkDiameter"): setx(pid, _scalar_expr(csink_dia))
        for pid in ("cSinkAngleV3", "cSinkAngle"): setx(pid, _scalar_expr(csink_angle, "deg"))
    return {"feature": feat}


_SOLID_BODIES_FS = ("function(context is Context, queries){ var o=[]; "
                    "for (var x in evaluateQuery(context, qBodyType(qEverything(EntityType.BODY), BodyType.SOLID)))"
                    "{ o=append(o, transientQueriesToStrings(x)); } return o; }")

async def _native_hole(doc, ws, elem, plane, centers, style, diameter, depth, name,
                       up=False, through=False, **kw):
    """Drive the native Hole feature: a points sketch at the centers, then the hole referencing
    those points. Returns (add_feature response, points-sketch featureId)."""
    sk = SketchSession(doc, ws, elem, plane, f"{name} locations")
    for c in centers:
        sk.add_point(tuple(c))
    rs = await PS.add_feature(doc, ws, elem, sk.build())
    sfid = rs["feature"]["featureId"]
    loc_ids = sel.parse_ids(await FS.evaluate(doc, ws, elem, sel.fs_sketch_vertices(sfid)))
    locq = {"btType": "BTMIndividualQuery-138",
            "queryString": f'query=qCreatedBy(makeId("{sfid}"), EntityType.VERTEX);',
            "deterministicIds": loc_ids}
    scope = sel.parse_ids(await FS.evaluate(doc, ws, elem, _SOLID_BODIES_FS))
    feat = _hole_native_json(locq, scope, style, diameter, depth, name, up=up, through=through, **kw)
    r = await PS.add_feature(doc, ws, elem, feat)
    return r, sfid


# Feature-based pattern/mirror: repeat whole FEATURES (patternType=FEATURE + instanceFunction),
# not faces. Verified against hand-built examples — face-based variants errored on regenerate.
def _mirror_json(feature_ids, plane_id: str, name: str) -> Dict[str, Any]:
    return _feat("mirror", name, [
        _enum("patternType", "MirrorType", "FEATURE"),
        _enum("operationType", "NewBodyOperationType", "NEW"),
        _featlist("instanceFunction", feature_ids),
        _qlist("mirrorPlane", [plane_id])])

def _linear_pattern_json(feature_ids, direction_id: str, distance, count, name: str) -> Dict[str, Any]:
    return _feat("linearPattern", name, [
        _enum("patternType", "PatternType", "FEATURE"),
        _enum("operationType", "NewBodyOperationType", "NEW"),
        _featlist("instanceFunction", feature_ids),
        _qlist("directionOne", [direction_id]),
        _qty("distance", _scalar_expr(distance)),
        _qty("instanceCount", _count_expr(count), integer=True)])

def _circular_pattern_json(feature_ids, axis_id: str, count, angle, name: str) -> Dict[str, Any]:
    return _feat("circularPattern", name, [
        _enum("patternType", "PatternType", "FEATURE"),
        _enum("operationType", "NewBodyOperationType", "NEW"),
        _featlist("instanceFunction", feature_ids),
        _qlist("axis", [axis_id]),
        _qty("angle", _scalar_expr(angle, "deg")),
        _qty("instanceCount", _count_expr(count), integer=True), _flag("equalSpace", True)])

# --------------------------------------------------------------------------
server = Server("cadkit")

@server.list_tools()
async def list_tools() -> List[Tool]:
    ds = {"documentId": {"type": "string"}, "workspaceId": {"type": "string"}, "elementId": {"type": "string"}}
    pt = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}
    return [
        Tool(name="cad_document_create", description="Create a document and return documentId + the Main workspaceId.",
             inputSchema={"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}},
                          "required": ["name"]}),
        Tool(name="cad_part_studio_create", description="Create a Part Studio; returns elementId.",
             inputSchema={"type": "object", "properties": {**ds, "name": {"type": "string"}},
                          "required": ["documentId", "workspaceId", "name"]}),
        Tool(name="cad_sketch_begin", description="Open a sketch session on a standard plane (Front/Top/Right) OR an existing "
             "face (pass its deterministic id from cad_find_faces as `face`). Returns a sessionId.",
             inputSchema={"type": "object", "properties": {**ds, "plane": {"type": "string", "enum": list(PLANES)},
                          "face": {"type": "string"}, "name": {"type": "string"}},
                          "required": ["documentId", "workspaceId", "elementId"]}),
        Tool(name="cad_sketch_line", description="Add a line; returns its entityId (points are <id>.start / <id>.end).",
             inputSchema={"type": "object", "properties": {"sessionId": {"type": "string"}, "start": pt, "end": pt,
                          "construction": {"type": "boolean"}}, "required": ["sessionId", "start", "end"]}),
        Tool(name="cad_sketch_circle", description="Add a circle; returns entityId. construction=true for a reference circle.",
             inputSchema={"type": "object", "properties": {"sessionId": {"type": "string"}, "center": pt,
                          "radius": {"type": "number"}, "construction": {"type": "boolean"}},
                          "required": ["sessionId", "center", "radius"]}),
        Tool(name="cad_sketch_arc", description="Add a center-point arc swept counterclockwise from `start` to `end`; "
             "returns entityId (points are <id>.start / <id>.end). Radius is set by `start`; the end snaps to that "
             "radius. Swap start/end for the complementary arc. construction=true for a reference arc.",
             inputSchema={"type": "object", "properties": {"sessionId": {"type": "string"}, "center": pt,
                          "start": pt, "end": pt, "construction": {"type": "boolean"}},
                          "required": ["sessionId", "center", "start", "end"]}),
        Tool(name="cad_sketch_fillet", description="Round the corner where two lines meet with a tangent arc of `radius` "
             "(inches). Trims both lines to the tangent points, drops the corner coincident, inserts the arc, and adds "
             "tangent constraints. Returns {arc, center, tangentPoints, radius}.",
             inputSchema={"type": "object", "properties": {"sessionId": {"type": "string"}, "line1": {"type": "string"},
                          "line2": {"type": "string"}, "radius": {"type": "number"}},
                          "required": ["sessionId", "line1", "line2", "radius"]}),
        Tool(name="cad_sketch_mirror", description="Mirror sketch lines across an existing line entity (the axis — e.g. a "
             "construction line). Emits the reflected copies and a MIRROR constraint tying each copy to its original. "
             "Lines only for now. Returns {originalId: copyId}.",
             inputSchema={"type": "object", "properties": {"sessionId": {"type": "string"},
                          "entityIds": {"type": "array", "items": {"type": "string"}}, "axis": {"type": "string"}},
                          "required": ["sessionId", "entityIds", "axis"]}),
        Tool(name="cad_sketch_rectangle", description="Add a constrained rectangle; returns {bottom,right,top,left} line ids.",
             inputSchema={"type": "object", "properties": {"sessionId": {"type": "string"}, "corner1": pt, "corner2": pt},
                          "required": ["sessionId", "corner1", "corner2"]}),
        Tool(name="cad_sketch_slot", description="Add an obround (rounded slot) between two centre points, given width — "
             "a rectangle + a circle at each end; extruding unions them into a clean slot. Returns {sides, caps} ids.",
             inputSchema={"type": "object", "properties": {"sessionId": {"type": "string"}, "center1": pt, "center2": pt,
                          "width": {"type": "number"}}, "required": ["sessionId", "center1", "center2", "width"]}),
        Tool(name="cad_sketch_polyline", description="Add a chain of lines through points. Auto coincident-joins them; "
             "closed=True closes the loop; auto_hv applies horizontal/vertical to axis-aligned segments; "
             "ground_first grounds the first point to the origin if it is at (0,0). Returns the line ids.",
             inputSchema={"type": "object", "properties": {"sessionId": {"type": "string"},
                          "points": {"type": "array", "items": pt},
                          "closed": {"type": "boolean"}, "auto_hv": {"type": "boolean"}, "ground_first": {"type": "boolean"}},
                          "required": ["sessionId", "points"]}),
        Tool(name="cad_sketch_constrain", description="Add a geometric constraint. type one of: coincident, horizontal, "
             "vertical, parallel, perpendicular, tangent, equal, concentric, pierce, midpoint, symmetric, fix, "
             "ground_origin. 'a'/'b'(/'c' for symmetry line) are entity/point ids like 'ln1' or 'ln1.start'. "
             "ground_origin grounds point 'a' to the part-studio origin.",
             inputSchema={"type": "object", "properties": {"sessionId": {"type": "string"}, "type": {"type": "string"},
                          "a": {"type": "string"}, "b": {"type": "string"}, "c": {"type": "string"}},
                          "required": ["sessionId", "type", "a"]}),
        Tool(name="cad_sketch_dimension", description="Add a driving dimension. kind: length (line), radius/diameter "
             "(circle), distance (entity+entity2), angle (line+line, value in degrees). value is inches (number) or an "
             "expression/#variable (e.g. '#base_len', '60 mm').",
             inputSchema={"type": "object", "properties": {"sessionId": {"type": "string"},
                          "kind": {"type": "string", "enum": ["length", "radius", "diameter", "distance", "angle"]},
                          "entity": {"type": "string"}, "entity2": {"type": "string"}, "value": {}},
                          "required": ["sessionId", "kind", "entity", "value"]}),
        Tool(name="cad_sketch_close", description="Post the sketch as one feature; returns its featureId plus diagnostics "
             "(grounded, dimensions, wellFormed). Set require_well_formed=true to refuse (without posting) a sketch that "
             "is ungrounded or has no driving dimensions.",
             inputSchema={"type": "object", "properties": {"sessionId": {"type": "string"},
                          "require_well_formed": {"type": "boolean"}}, "required": ["sessionId"]}),
        Tool(name="cad_set_variable", description="Set a part-studio variable (assignVariable), update-or-create: re-setting "
             "the same name updates it in place instead of adding a duplicate. expression e.g. '2.4 in' or '#other*2'.",
             inputSchema={"type": "object", "properties": {**ds, "name": {"type": "string"}, "expression": {"type": "string"}},
                          "required": ["documentId", "workspaceId", "elementId", "name", "expression"]}),
        Tool(name="cad_extrude", description="Extrude a sketch region. operation: NEW/ADD/REMOVE/INTERSECT. depth is inches "
             "(number) or an expression/#variable.",
             inputSchema={"type": "object", "properties": {**ds, "sketchFeatureId": {"type": "string"},
                          "depth": {"type": ["number", "string"]}, "operation": {"type": "string", "enum": ["NEW","ADD","REMOVE","INTERSECT"]},
                          "name": {"type": "string"}}, "required": ["documentId","workspaceId","elementId","sketchFeatureId","depth"]}),
        Tool(name="cad_fillet", description="Fillet edges (deterministic ids from cad_find_edges). radius is inches (number) "
             "or an expression/#variable.",
             inputSchema={"type": "object", "properties": {**ds, "edgeIds": {"type": "array", "items": {"type": "string"}},
                          "radius": {"type": ["number", "string"]}, "name": {"type": "string"}},
                          "required": ["documentId","workspaceId","elementId","edgeIds","radius"]}),
        Tool(name="cad_find_edges", description="Find edges by geometry. kind: circular (radius+tol), concave/convex (inner/"
             "outer corners — concave is ideal for fillets), linear (axis X/Y/Z and/or through point), extreme (ALL edges "
             "furthest along axis — e.g. axis=Z max=false for the bottom edges, to fillet at once), on_plane (all edges "
             "lying in the plane axis=coordinate, e.g. axis=Z coordinate=0). Returns deterministic ids.",
             inputSchema={"type": "object", "properties": {**ds,
                          "kind": {"type": "string", "enum": ["circular","concave","convex","linear","extreme","on_plane"]},
                          "radius": {"type": "number"}, "tolerance": {"type": "number"}, "axis": {"type": "string", "enum": ["X","Y","Z"]},
                          "max": {"type": "boolean"}, "through": {"type": "array", "items": {}}, "coordinate": {"type": "number"}},
                          "required": ["documentId","workspaceId","elementId","kind"]}),
        Tool(name="cad_find_faces", description="Find faces by geometry. kind: planar_by_normal (normal=[x,y,z]), cylindrical "
             "(radius+tol), largest/smallest (by area — e.g. the big flat face to sketch on), extreme (the face furthest "
             "along axis — axis=Z max=true is the top face), adjacent_to_extreme (the faces bordering that extreme face), "
             "on_plane (the planar face lying in axis=coordinate, e.g. axis=Z coordinate=0). Returns deterministic ids.",
             inputSchema={"type": "object", "properties": {**ds,
                          "kind": {"type": "string", "enum": ["planar_by_normal","cylindrical","largest","smallest","extreme","adjacent_to_extreme","on_plane"]},
                          "normal": {"type": "array", "items": {"type": "number"}}, "radius": {"type": "number"}, "coordinate": {"type": "number"},
                          "axis": {"type": "string", "enum": ["X","Y","Z"]}, "max": {"type": "boolean"},
                          "tolerance": {"type": "number"}}, "required": ["documentId","workspaceId","elementId","kind"]}),
        Tool(name="cad_chamfer", description="Equal-distance chamfer on edges (deterministic ids from cad_find_edges). "
             "distance is inches (number) or an expression/#variable.",
             inputSchema={"type": "object", "properties": {**ds, "edgeIds": {"type": "array", "items": {"type": "string"}},
                          "distance": {"type": ["number", "string"]}, "name": {"type": "string"}},
                          "required": ["documentId","workspaceId","elementId","edgeIds","distance"]}),
        Tool(name="cad_hole", description="Hole(s) at the given centers on a plane/face. diameter/depth accept numbers or "
             "#variables. plane is Front/Top/Right OR a face id (from cad_find_faces). style: 'simple' (default) | "
             "'counterbore' (needs cboreDiameter + cboreDepth) | 'countersink' (needs csinkDiameter; csinkAngle default 90) "
             "— counterbore/countersink use the native Hole feature (proper callouts). Set up=true if the hole reports "
             "'none intersected' (flips the drill direction). through=true for a through-all hole.",
             inputSchema={"type": "object", "properties": {**ds, "plane": {"type": "string"},
                          "centers": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
                          "diameter": {"type": ["number", "string"]}, "depth": {"type": ["number", "string"]},
                          "style": {"type": "string", "enum": ["simple", "counterbore", "countersink"]},
                          "cboreDiameter": {"type": ["number", "string"]}, "cboreDepth": {"type": ["number", "string"]},
                          "csinkDiameter": {"type": ["number", "string"]}, "csinkAngle": {"type": ["number", "string"]},
                          "up": {"type": "boolean"}, "through": {"type": "boolean"}, "name": {"type": "string"}},
                          "required": ["documentId","workspaceId","elementId","plane","centers","diameter","depth"]}),
        Tool(name="cad_revolve", description="Revolve a sketch region about an axis edge. angle in degrees (number/#var); "
             "omit angle for a full 360 revolve. operation: NEW/ADD/REMOVE/INTERSECT.",
             inputSchema={"type": "object", "properties": {**ds, "sketchFeatureId": {"type": "string"},
                          "axisId": {"type": "string"}, "angle": {"type": ["number", "string"]},
                          "operation": {"type": "string", "enum": ["NEW","ADD","REMOVE","INTERSECT"]}, "name": {"type": "string"}},
                          "required": ["documentId","workspaceId","elementId","sketchFeatureId","axisId"]}),
        Tool(name="cad_shell", description="Hollow the solid by removing the given faces and leaving a wall. thickness is "
             "inches (number) or #variable; wall is inward.",
             inputSchema={"type": "object", "properties": {**ds, "faceIds": {"type": "array", "items": {"type": "string"}},
                          "thickness": {"type": ["number", "string"]}, "name": {"type": "string"}},
                          "required": ["documentId","workspaceId","elementId","faceIds","thickness"]}),
        Tool(name="cad_mirror", description="Mirror whole features across a plane. featureIds = the features to "
             "repeat (e.g. an extrude/hole featureId); planeId = Front/Top/Right or a face/plane id.",
             inputSchema={"type": "object", "properties": {**ds, "featureIds": {"type": "array", "items": {"type": "string"}},
                          "planeId": {"type": "string"}, "name": {"type": "string"}},
                          "required": ["documentId","workspaceId","elementId","featureIds","planeId"]}),
        Tool(name="cad_pattern", description="Pattern whole features. kind=linear needs directionId (an edge) + spacing "
             "(number/#var) + count; kind=circular needs axisId (an edge) + count, evenly spaced over angle (default 360). "
             "featureIds = the features to repeat.",
             inputSchema={"type": "object", "properties": {**ds, "kind": {"type": "string", "enum": ["linear","circular"]},
                          "featureIds": {"type": "array", "items": {"type": "string"}},
                          "directionId": {"type": "string"}, "axisId": {"type": "string"},
                          "spacing": {"type": ["number", "string"]}, "angle": {"type": ["number", "string"]},
                          "count": {"type": ["integer", "string"]}, "name": {"type": "string"}},
                          "required": ["documentId","workspaceId","elementId","kind","featureIds","count"]}),
        # ---- P2: inspection, lifecycle, I/O ----
        Tool(name="cad_measure", description="Measure the part studio's solids in ONE FeatureScript eval: solid count, "
             "total volume (in^3), and the combined bounding box (min/max/size in inches). Quota-frugal.",
             inputSchema={"type": "object", "properties": {**ds}, "required": ["documentId","workspaceId","elementId"]}),
        Tool(name="cad_get_variables", description="List the cadkit variables in this part studio (name + authored "
             "expression, e.g. '#leg' = '2 in'). Reads the assignVariable features; one API call.",
             inputSchema={"type": "object", "properties": {**ds}, "required": ["documentId","workspaceId","elementId"]}),
        Tool(name="cad_delete_feature", description="Delete a feature by featureId.",
             inputSchema={"type": "object", "properties": {**ds, "featureId": {"type": "string"}},
                          "required": ["documentId","workspaceId","elementId","featureId"]}),
        Tool(name="cad_suppress", description="Suppress (suppressed=true) or unsuppress (false) a feature by featureId, "
             "leaving it in the tree.",
             inputSchema={"type": "object", "properties": {**ds, "featureId": {"type": "string"},
                          "suppressed": {"type": "boolean"}},
                          "required": ["documentId","workspaceId","elementId","featureId","suppressed"]}),
        Tool(name="cad_edit_feature", description="Edit one stored parameter of an existing feature. Give parameterId "
             "(e.g. 'depth','radius','length') plus expression (number/#var for a quantity) OR value (enum/bool/string). "
             "Retarget a dimension to a #variable without rebuilding.",
             inputSchema={"type": "object", "properties": {**ds, "featureId": {"type": "string"},
                          "parameterId": {"type": "string"}, "expression": {"type": ["number","string"]},
                          "value": {}},
                          "required": ["documentId","workspaceId","elementId","featureId","parameterId"]}),
        Tool(name="cad_export", description="Export the part studio to a file format. format: STL/STEP/PARASOLID/GLTF/OBJ "
             "(default STEP). Optional partId to export a single part. Returns the translation result.",
             inputSchema={"type": "object", "properties": {**ds, "format": {"type": "string"}, "partId": {"type": "string"}},
                          "required": ["documentId","workspaceId","elementId"]}),
        Tool(name="cad_api_calls", description="Report how many SUCCESSFUL Onshape API calls (2xx/3xx — the ones that count "
             "against the 2,500/user/yr budget) cadkit has made this session. Costs no API call. Check it before/after "
             "live work to keep quota spend visible.",
             inputSchema={"type": "object", "properties": {}}),
    ]

@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    return await dispatch(name, arguments)

async def dispatch(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    a = arguments
    try:
        if name == "cad_document_create":
            info = await DOCS.create_document(a["name"], a.get("description"))
            client._ensure_client()
            ws = (await client._client.get(
                f"https://cad.onshape.com/api/v6/documents/d/{info.id}/workspaces",
                headers={"Authorization": client._get_auth_header(), "Accept": "application/json"})).json()
            main = next((w["id"] for w in ws if w.get("isMain")), ws[0]["id"])
            return _txt(json.dumps({"documentId": info.id, "workspaceId": main}))

        if name == "cad_part_studio_create":
            r = await PS.create_part_studio(a["documentId"], a["workspaceId"], a["name"])
            return _txt(json.dumps({"elementId": r.get("id")}))

        if name == "cad_sketch_begin":
            sid = _new_session_id()
            target = a.get("face") or a.get("plane", "Front")  # face id overrides standard plane
            SESSIONS[sid] = SketchSession(a["documentId"], a["workspaceId"], a["elementId"],
                                          target, a.get("name", "Sketch"))
            return _txt(json.dumps({"sessionId": sid}))

        if name in ("cad_sketch_line","cad_sketch_circle","cad_sketch_arc","cad_sketch_fillet","cad_sketch_mirror",
                    "cad_sketch_rectangle","cad_sketch_polyline","cad_sketch_slot","cad_sketch_constrain",
                    "cad_sketch_dimension","cad_sketch_close"):
            s = SESSIONS.get(a.get("sessionId"))
            if not s:
                return _txt(f"ERROR: unknown sessionId {a.get('sessionId')}")
            if name == "cad_sketch_line":
                return _txt(json.dumps({"entityId": s.add_line(a["start"], a["end"], a.get("construction", False))}))
            if name == "cad_sketch_circle":
                return _txt(json.dumps({"entityId": s.add_circle(a["center"], a["radius"], a.get("construction", False))}))
            if name == "cad_sketch_arc":
                return _txt(json.dumps({"entityId": s.add_arc(a["center"], a["start"], a["end"], a.get("construction", False))}))
            if name == "cad_sketch_fillet":
                return _txt(json.dumps(s.add_fillet(a["line1"], a["line2"], a["radius"])))
            if name == "cad_sketch_mirror":
                return _txt(json.dumps(s.add_mirror(a["entityIds"], a["axis"])))
            if name == "cad_sketch_rectangle":
                return _txt(json.dumps(s.add_rectangle(a["corner1"], a["corner2"])))
            if name == "cad_sketch_slot":
                return _txt(json.dumps(s.add_slot(a["center1"], a["center2"], a["width"])))
            if name == "cad_sketch_polyline":
                pts = a["points"]; closed = a.get("closed", True); auto_hv = a.get("auto_hv", True)
                ground_first = a.get("ground_first", True)
                ids = []
                seq = list(range(len(pts)))
                segs = [(pts[i], pts[(i+1) % len(pts)]) for i in seq] if closed else \
                       [(pts[i], pts[i+1]) for i in range(len(pts)-1)]
                for st, en in segs:
                    lid = s.add_line(st, en); ids.append(lid)
                    if auto_hv:
                        if abs(en[0]-st[0]) < 1e-9: s.vertical(lid)
                        elif abs(en[1]-st[1]) < 1e-9: s.horizontal(lid)
                for i in range(len(ids)-1):
                    s.coincident(f"{ids[i]}.end", f"{ids[i+1]}.start")
                if closed:
                    s.coincident(f"{ids[-1]}.end", f"{ids[0]}.start")
                if ground_first and abs(pts[0][0]) < 1e-9 and abs(pts[0][1]) < 1e-9:
                    s.ground_origin(f"{ids[0]}.start")
                return _txt(json.dumps({"lineIds": ids}))
            if name == "cad_sketch_constrain":
                s.constrain(a["type"], a["a"], a.get("b"), a.get("c"))
                return _txt("ok")
            if name == "cad_sketch_dimension":
                k = a["kind"]; e = a["entity"]; v = a["value"]; e2 = a.get("entity2")
                {"length": lambda: s.dim_length(e, v), "radius": lambda: s.dim_radius(e, v),
                 "diameter": lambda: s.dim_diameter(e, v), "distance": lambda: s.dim_distance(e, e2, v),
                 "angle": lambda: s.dim_angle(e, e2, v)}[k]()
                return _txt("ok")
            if name == "cad_sketch_close":
                diag = s.diagnostics()
                if a.get("require_well_formed") and not diag["wellFormed"]:
                    # fail BEFORE posting (also saves a call): say what's missing
                    missing = []
                    if not diag["grounded"]: missing.append("not grounded to origin")
                    if diag["dimensions"] == 0: missing.append("no driving dimensions")
                    return _txt(json.dumps({"error": "sketch under-defined: " + "; ".join(missing),
                                            "diagnostics": diag}))
                r = await PS.add_feature(s.doc, s.ws, s.elem, s.build())
                fid = r["feature"]["featureId"]; st = r.get("featureState", {}).get("featureStatus")
                del SESSIONS[a["sessionId"]]
                out = {"sketchFeatureId": fid, "status": st, **diag}
                if not diag["wellFormed"]:
                    out["warning"] = ("likely under-defined ("
                                      + ("ungrounded" if not diag["grounded"] else "")
                                      + (" " if not diag["grounded"] and diag["dimensions"] == 0 else "")
                                      + ("no dimensions" if diag["dimensions"] == 0 else "") + ")")
                return _txt(json.dumps(out))

        if name == "cad_set_variable":
            r = await _set_variable(a["documentId"], a["workspaceId"], a["elementId"],
                                    a["name"], a["expression"])
            return _txt(json.dumps(r))

        if name == "cad_extrude":
            r = await PS.add_feature(a["documentId"], a["workspaceId"], a["elementId"],
                _extrude_json(a["sketchFeatureId"], a["depth"], a.get("operation", "NEW"), a.get("name", "Extrude")))
            return _feat_result(r)

        if name == "cad_fillet":
            r = await PS.add_feature(a["documentId"], a["workspaceId"], a["elementId"],
                _fillet_json(a["edgeIds"], a["radius"], a.get("name", "Fillet")))
            return _feat_result(r)

        if name == "cad_find_edges":
            kind = a["kind"]; tol = a.get("tolerance", 0.001)
            if kind == "circular": script = sel.fs_circular_edges(a.get("radius"), tol)
            elif kind in ("concave", "convex"): script = sel.fs_concave_edges(kind.upper())
            elif kind == "extreme": script = sel.fs_extreme_edges(a["axis"], a.get("max", True), a.get("tolerance", 0.01))
            elif kind == "on_plane": script = sel.fs_edges_on_plane(a["axis"], a["coordinate"], a.get("tolerance", 0.01))
            else: script = sel.fs_linear_edges(a.get("axis"), a.get("through"), a.get("tolerance", 0.005))
            res = await FS.evaluate(a["documentId"], a["workspaceId"], a["elementId"], script)
            return _txt(json.dumps({"edgeIds": sel.parse_ids(res)}))

        if name == "cad_find_faces":
            kind = a["kind"]
            if kind == "planar_by_normal": script = sel.fs_planar_faces_by_normal(a["normal"], a.get("tolerance", 1e-3))
            elif kind == "cylindrical": script = sel.fs_cylindrical_faces(a.get("radius"), a.get("tolerance", 0.001))
            elif kind in ("largest", "smallest"): script = sel.fs_faces_by_area(kind == "largest")
            elif kind == "extreme": script = sel.fs_extreme_faces(a["axis"], a.get("max", True))
            elif kind == "adjacent_to_extreme": script = sel.fs_faces_adjacent_to_extreme(a["axis"], a.get("max", True))
            elif kind == "on_plane": script = sel.fs_faces_on_plane(a["axis"], a["coordinate"], a.get("tolerance", 0.01))
            else: return _txt(json.dumps({"error": f"unknown face kind '{kind}'"}))
            res = await FS.evaluate(a["documentId"], a["workspaceId"], a["elementId"], script)
            return _txt(json.dumps({"faceIds": sel.parse_ids(res)}))

        if name == "cad_chamfer":
            r = await PS.add_feature(a["documentId"], a["workspaceId"], a["elementId"],
                _chamfer_json(a["edgeIds"], a["distance"], a.get("name", "Chamfer")))
            return _feat_result(r)

        if name == "cad_hole":
            # simple -> a light circle + REMOVE extrude. counterbore/countersink -> the native
            # Hole feature (proper hole, callouts, exact profile). `up` flips the drill direction
            # so it cuts INTO the part (set up=true if the result reports "none intersected").
            doc, ws, elem = a["documentId"], a["workspaceId"], a["elementId"]
            plane, centers, nm = a["plane"], a["centers"], a.get("name", "Hole")
            style = a.get("style", "simple")
            if style == "simple":
                sk = SketchSession(doc, ws, elem, plane, f"{nm} sketch")
                for c in centers:
                    cid = sk.add_circle(tuple(c), 0.5)        # radius refined by the diameter dim
                    sk.dim_diameter(cid, a["diameter"])
                rs = await PS.add_feature(doc, ws, elem, sk.build())
                sfid = rs["feature"]["featureId"]
                re = await PS.add_feature(doc, ws, elem,
                    _extrude_json(sfid, a["depth"], "REMOVE", nm))
                return _feat_result(re, sketchFeatureId=sfid)
            if style == "counterbore":
                if not (a.get("cboreDiameter") and a.get("cboreDepth")):
                    return _txt(json.dumps({"error": "style=counterbore needs cboreDiameter + cboreDepth"}))
                kw = {"cbore_dia": a["cboreDiameter"], "cbore_depth": a["cboreDepth"]}
            elif style == "countersink":
                if not a.get("csinkDiameter"):
                    return _txt(json.dumps({"error": "style=countersink needs csinkDiameter"}))
                kw = {"csink_dia": a["csinkDiameter"], "csink_angle": a.get("csinkAngle", 90)}
            else:
                return _txt(json.dumps({"error": f"unknown hole style '{style}' (simple|counterbore|countersink)"}))
            r, sfid = await _native_hole(doc, ws, elem, plane, centers, style, a["diameter"], a["depth"],
                                         nm, up=a.get("up", False), through=a.get("through", False), **kw)
            return _feat_result(r, sketchFeatureId=sfid)

        if name == "cad_revolve":
            r = await PS.add_feature(a["documentId"], a["workspaceId"], a["elementId"],
                _revolve_json(a["sketchFeatureId"], a["axisId"], a.get("angle"),
                              a.get("operation", "NEW"), a.get("name", "Revolve")))
            return _feat_result(r)

        if name == "cad_shell":
            r = await PS.add_feature(a["documentId"], a["workspaceId"], a["elementId"],
                _shell_json(a["faceIds"], a["thickness"], a.get("name", "Shell")))
            return _feat_result(r)

        if name == "cad_mirror":
            pid = PLANES.get(a["planeId"], a["planeId"])     # accept Front/Top/Right or a face id
            r = await PS.add_feature(a["documentId"], a["workspaceId"], a["elementId"],
                _mirror_json(a["featureIds"], pid, a.get("name", "Mirror")))
            return _feat_result(r)

        if name == "cad_pattern":
            if a["kind"] == "linear":
                j = _linear_pattern_json(a["featureIds"], a["directionId"], a.get("spacing", 1.0),
                                         a["count"], a.get("name", "Pattern"))
            else:
                j = _circular_pattern_json(a["featureIds"], a["axisId"], a["count"],
                                           a.get("angle", 360), a.get("name", "Pattern"))
            r = await PS.add_feature(a["documentId"], a["workspaceId"], a["elementId"], j)
            return _feat_result(r)

        # ---- P2: inspection, lifecycle, I/O ----
        if name == "cad_measure":
            res = await FS.evaluate(a["documentId"], a["workspaceId"], a["elementId"], measure_fs())
            parsed = parse_fs(res.get("result", res))
            return _txt(json.dumps(_measure_summary(parsed)))

        if name == "cad_get_variables":
            feats = await PS.get_features(a["documentId"], a["workspaceId"], a["elementId"])
            return _txt(json.dumps({"variables": _scan_variables(feats.get("features", []))}))

        if name == "cad_delete_feature":
            await PS.delete_feature(a["documentId"], a["workspaceId"], a["elementId"], a["featureId"])
            _VAR_CACHE.pop((a["documentId"], a["workspaceId"], a["elementId"]), None)  # tree changed
            return _txt(json.dumps({"status": "deleted", "featureId": a["featureId"]}))

        if name == "cad_suppress":
            feats = await PS.get_features(a["documentId"], a["workspaceId"], a["elementId"])
            feat = next((f for f in feats.get("features", []) if f.get("featureId") == a["featureId"]), None)
            if feat is None:
                return _txt(json.dumps({"error": f"feature {a['featureId']} not found"}))
            feat["suppressed"] = bool(a["suppressed"])
            r = await PS.update_feature(a["documentId"], a["workspaceId"], a["elementId"],
                                        a["featureId"], {"feature": feat})
            return _feat_result(r, suppressed=bool(a["suppressed"]))

        if name == "cad_edit_feature":
            feats = await PS.get_features(a["documentId"], a["workspaceId"], a["elementId"])
            feat = next((f for f in feats.get("features", []) if f.get("featureId") == a["featureId"]), None)
            if feat is None:
                return _txt(json.dumps({"error": f"feature {a['featureId']} not found"}))
            expr = a.get("expression")
            expr = _scalar_expr(expr) if isinstance(expr, (int, float)) else expr
            _apply_param_edit(feat, a["parameterId"], expression=expr, value=a.get("value"))
            r = await PS.update_feature(a["documentId"], a["workspaceId"], a["elementId"],
                                        a["featureId"], {"feature": feat})
            return _feat_result(r)

        if name == "cad_export":
            r = await EXPORT.export_part_studio(a["documentId"], a["workspaceId"], a["elementId"],
                                                a.get("format", "STEP"), a.get("partId"))
            return _txt(json.dumps(r) if isinstance(r, (dict, list)) else json.dumps({"result": str(r)}))

        if name == "cad_api_calls":
            return _txt(json.dumps(quota.counts()))

        return _txt(f"ERROR: unknown tool {name}")
    except Exception as e:
        return _txt(f"ERROR in {name}: {e}")

async def main_stdio():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())

def main():
    asyncio.run(main_stdio())

if __name__ == "__main__":
    main()
