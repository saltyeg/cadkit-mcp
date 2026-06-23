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
from onshape_mcp.builders.fillet import FilletBuilder

from .sketch import SketchSession, PLANES
from . import selection as sel

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
PS = PartStudioManager(client)
FS = FeatureScriptManager(client)
DOCS = DocumentManager(client)

SESSIONS: Dict[str, SketchSession] = {}
_counter = {"n": 0}

def _new_session_id() -> str:
    _counter["n"] += 1
    return f"sk{_counter['n']}"

def _txt(s: str) -> List[TextContent]:
    return [TextContent(type="text", text=s)]

def _extrude_json(sketch_fid: str, depth_in: float, op: str, name: str) -> Dict[str, Any]:
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
            {"btType": "BTMParameterQuantity-147", "expression": f"{depth_in} in",
             "parameterId": "depth", "isInteger": False}]}}

def _assign_variable_json(name: str, expression: str) -> Dict[str, Any]:
    return {"feature": {"btType": "BTMFeature-134", "featureType": "assignVariable",
        "name": name, "suppressed": False, "namespace": "",
        "parameters": [{"btType": "BTMParameterString-149", "value": name, "parameterId": "name"},
                       {"btType": "BTMParameterQuantity-147", "isInteger": False,
                        "expression": expression, "parameterId": "value"}]}}

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
        Tool(name="cad_sketch_begin", description="Open a sketch session on a plane (Front/Top/Right). Returns a sessionId.",
             inputSchema={"type": "object", "properties": {**ds, "plane": {"type": "string", "enum": list(PLANES)},
                          "name": {"type": "string"}}, "required": ["documentId", "workspaceId", "elementId"]}),
        Tool(name="cad_sketch_line", description="Add a line; returns its entityId (points are <id>.start / <id>.end).",
             inputSchema={"type": "object", "properties": {"sessionId": {"type": "string"}, "start": pt, "end": pt,
                          "construction": {"type": "boolean"}}, "required": ["sessionId", "start", "end"]}),
        Tool(name="cad_sketch_circle", description="Add a circle; returns entityId.",
             inputSchema={"type": "object", "properties": {"sessionId": {"type": "string"}, "center": pt,
                          "radius": {"type": "number"}}, "required": ["sessionId", "center", "radius"]}),
        Tool(name="cad_sketch_rectangle", description="Add a constrained rectangle; returns {bottom,right,top,left} line ids.",
             inputSchema={"type": "object", "properties": {"sessionId": {"type": "string"}, "corner1": pt, "corner2": pt},
                          "required": ["sessionId", "corner1", "corner2"]}),
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
        Tool(name="cad_sketch_close", description="Post the sketch as one feature; returns its featureId.",
             inputSchema={"type": "object", "properties": {"sessionId": {"type": "string"}}, "required": ["sessionId"]}),
        Tool(name="cad_set_variable", description="Create/assign a part-studio variable (assignVariable). expression e.g. '2.4 in'.",
             inputSchema={"type": "object", "properties": {**ds, "name": {"type": "string"}, "expression": {"type": "string"}},
                          "required": ["documentId", "workspaceId", "elementId", "name", "expression"]}),
        Tool(name="cad_extrude", description="Extrude a sketch region. operation: NEW/ADD/REMOVE/INTERSECT.",
             inputSchema={"type": "object", "properties": {**ds, "sketchFeatureId": {"type": "string"},
                          "depth": {"type": "number"}, "operation": {"type": "string", "enum": ["NEW","ADD","REMOVE","INTERSECT"]},
                          "name": {"type": "string"}}, "required": ["documentId","workspaceId","elementId","sketchFeatureId","depth"]}),
        Tool(name="cad_fillet", description="Fillet edges (deterministic ids from cad_find_edges).",
             inputSchema={"type": "object", "properties": {**ds, "edgeIds": {"type": "array", "items": {"type": "string"}},
                          "radius": {"type": "number"}, "name": {"type": "string"}},
                          "required": ["documentId","workspaceId","elementId","edgeIds","radius"]}),
        Tool(name="cad_find_edges", description="Find edges by geometry. kind: circular (radius+tol), concave (inner "
             "corners, ideal for fillets), linear (axis X/Y/Z and/or through point). Returns deterministic ids.",
             inputSchema={"type": "object", "properties": {**ds, "kind": {"type": "string", "enum": ["circular","concave","convex","linear"]},
                          "radius": {"type": "number"}, "tolerance": {"type": "number"}, "axis": {"type": "string", "enum": ["X","Y","Z"]},
                          "through": {"type": "array", "items": {}}}, "required": ["documentId","workspaceId","elementId","kind"]}),
        Tool(name="cad_find_faces", description="Find faces by geometry. kind: planar_by_normal (normal=[x,y,z]) or "
             "cylindrical (radius+tol). Returns deterministic ids.",
             inputSchema={"type": "object", "properties": {**ds, "kind": {"type": "string", "enum": ["planar_by_normal","cylindrical"]},
                          "normal": {"type": "array", "items": {"type": "number"}}, "radius": {"type": "number"},
                          "tolerance": {"type": "number"}}, "required": ["documentId","workspaceId","elementId","kind"]}),
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
            SESSIONS[sid] = SketchSession(a["documentId"], a["workspaceId"], a["elementId"],
                                          a.get("plane", "Front"), a.get("name", "Sketch"))
            return _txt(json.dumps({"sessionId": sid}))

        if name in ("cad_sketch_line","cad_sketch_circle","cad_sketch_rectangle","cad_sketch_polyline",
                    "cad_sketch_constrain","cad_sketch_dimension","cad_sketch_close"):
            s = SESSIONS.get(a.get("sessionId"))
            if not s:
                return _txt(f"ERROR: unknown sessionId {a.get('sessionId')}")
            if name == "cad_sketch_line":
                return _txt(json.dumps({"entityId": s.add_line(a["start"], a["end"], a.get("construction", False))}))
            if name == "cad_sketch_circle":
                return _txt(json.dumps({"entityId": s.add_circle(a["center"], a["radius"], a.get("construction", False))}))
            if name == "cad_sketch_rectangle":
                return _txt(json.dumps(s.add_rectangle(a["corner1"], a["corner2"])))
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
                r = await PS.add_feature(s.doc, s.ws, s.elem, s.build())
                fid = r["feature"]["featureId"]; st = r.get("featureState", {}).get("featureStatus")
                del SESSIONS[a["sessionId"]]
                return _txt(json.dumps({"sketchFeatureId": fid, "status": st}))

        if name == "cad_set_variable":
            r = await PS.add_feature(a["documentId"], a["workspaceId"], a["elementId"],
                                     _assign_variable_json(a["name"], a["expression"]))
            return _txt(json.dumps({"status": r.get("featureState", {}).get("featureStatus")}))

        if name == "cad_extrude":
            r = await PS.add_feature(a["documentId"], a["workspaceId"], a["elementId"],
                _extrude_json(a["sketchFeatureId"], a["depth"], a.get("operation", "NEW"), a.get("name", "Extrude")))
            return _txt(json.dumps({"status": r.get("featureState", {}).get("featureStatus")}))

        if name == "cad_fillet":
            fb = FilletBuilder(a.get("name", "Fillet"), a["radius"])
            for e in a["edgeIds"]: fb.add_edge(e)
            r = await PS.add_feature(a["documentId"], a["workspaceId"], a["elementId"], fb.build())
            return _txt(json.dumps({"status": r.get("featureState", {}).get("featureStatus")}))

        if name == "cad_find_edges":
            kind = a["kind"]; tol = a.get("tolerance", 0.001)
            if kind == "circular": script = sel.fs_circular_edges(a.get("radius"), tol)
            elif kind in ("concave", "convex"): script = sel.fs_concave_edges(kind.upper())
            else: script = sel.fs_linear_edges(a.get("axis"), a.get("through"), a.get("tolerance", 0.005))
            res = await FS.evaluate(a["documentId"], a["workspaceId"], a["elementId"], script)
            return _txt(json.dumps({"edgeIds": sel.parse_ids(res)}))

        if name == "cad_find_faces":
            if a["kind"] == "planar_by_normal": script = sel.fs_planar_faces_by_normal(a["normal"], a.get("tolerance", 1e-3))
            else: script = sel.fs_cylindrical_faces(a.get("radius"), a.get("tolerance", 0.001))
            res = await FS.evaluate(a["documentId"], a["workspaceId"], a["elementId"], script)
            return _txt(json.dumps({"faceIds": sel.parse_ids(res)}))

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
