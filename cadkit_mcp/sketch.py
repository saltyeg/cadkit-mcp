"""Stateful sketch-session builder for Onshape — the human design pattern.

One sketch accumulates entities + geometric constraints + driving dimensions,
then emits a single BTMSketch-151 feature.  Unlike coordinate-driven generators,
this produces *fully-defined* sketches: grounded to the origin and dimensioned.

All JSON shapes here were verified against the live Onshape API:
  - external refs use BTMParameterQueryList-148 / parameterId "externalFirst"
  - the part-studio origin vertex has deterministic id "IB"
  - default planes: Front=JCC, Top=JDC, Right=JEC
"""
import math
from typing import Any, Dict, List, Optional, Tuple

IN = 0.0254  # inches -> meters
PLANES = {"Front": "JCC", "Top": "JDC", "Right": "JEC"}
ORIGIN_VERTEX = "IB"


def _expr(value) -> str:
    """A dimension value: a number (inches), or a raw expression / #variable."""
    if isinstance(value, (int, float)):
        return f"{value} in"
    return str(value)  # e.g. "#base_len", "60 mm", "2.4*inch"


class SketchSession:
    def __init__(self, document_id, workspace_id, element_id,
                 plane: str = "Front", name: str = "Sketch"):
        if plane not in PLANES:
            raise ValueError(f"plane must be one of {list(PLANES)}")
        self.doc, self.ws, self.elem = document_id, workspace_id, element_id
        self.plane, self.name = plane, name
        self.entities: List[Dict[str, Any]] = []
        self.constraints: List[Dict[str, Any]] = []
        self._n = 0

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    # ---- entities ----------------------------------------------------------
    def add_line(self, start: Tuple[float, float], end: Tuple[float, float],
                 construction: bool = False) -> str:
        sx, sy = start; ex, ey = end
        length = math.hypot(ex - sx, ey - sy)
        if length == 0:
            raise ValueError("line endpoints must differ")
        eid = self._id("ln")
        self.entities.append({
            "btType": "BTMSketchCurveSegment-155", "entityId": eid,
            "startPointId": f"{eid}.start", "endPointId": f"{eid}.end",
            "startParam": 0.0, "endParam": length * IN,
            "geometry": {"btType": "BTCurveGeometryLine-117",
                         "pntX": sx * IN, "pntY": sy * IN,
                         "dirX": (ex - sx) / length, "dirY": (ey - sy) / length},
            "isConstruction": construction,
        })
        return eid

    def add_circle(self, center: Tuple[float, float], radius: float,
                   construction: bool = False) -> str:
        cx, cy = center
        cid = self._id("cir")
        # two semicircle segments form a closed, region-producing circle
        for tag, p0, p1 in (("a", 0.0, math.pi), ("b", math.pi, 2 * math.pi)):
            self.entities.append({
                "btType": "BTMSketchCurveSegment-155", "entityId": f"{cid}.{tag}",
                "startPointId": f"{cid}.{'s' if tag=='a' else 'm'}",
                "endPointId": f"{cid}.{'m' if tag=='a' else 's'}",
                "startParam": p0, "endParam": p1,
                "geometry": {"btType": "BTCurveGeometryCircle-115", "radius": radius * IN,
                             "xCenter": cx * IN, "yCenter": cy * IN,
                             "xDir": 1.0, "yDir": 0.0, "clockwise": False},
                "centerId": f"{cid}.center", "isConstruction": construction,
            })
        for tag, a, b in (("close1", f"{cid}.a.end", f"{cid}.b.start"),
                          ("close2", f"{cid}.b.end", f"{cid}.a.start")):
            self.constraints.append(self._con("COINCIDENT", f"{cid}.{tag}",
                [self._str(a, "localFirst"), self._str(b, "localSecond")]))
        return cid

    def add_rectangle(self, corner1, corner2):
        """Convenience: 4 lines + a full geometric-constraint set (origin NOT grounded)."""
        x1, y1 = corner1; x2, y2 = corner2
        b = self.add_line((x1, y1), (x2, y1))
        r = self.add_line((x2, y1), (x2, y2))
        t = self.add_line((x2, y2), (x1, y2))
        l = self.add_line((x1, y2), (x1, y1))
        self.coincident(f"{b}.end", f"{r}.start")
        self.coincident(f"{r}.end", f"{t}.start")
        self.coincident(f"{t}.end", f"{l}.start")
        self.coincident(f"{l}.end", f"{b}.start")
        self.horizontal(b); self.horizontal(t)
        self.vertical(l); self.vertical(r)
        return {"bottom": b, "right": r, "top": t, "left": l}

    # ---- constraint primitives --------------------------------------------
    @staticmethod
    def _str(v, pid): return {"btType": "BTMParameterString-149", "value": v, "parameterId": pid}

    @staticmethod
    def _extq(ids, pid):
        return {"btType": "BTMParameterQueryList-148", "parameterId": pid,
                "queries": [{"btType": "BTMIndividualQuery-138", "deterministicIds": ids}]}

    def _con(self, ctype, eid, params):
        return {"btType": "BTMSketchConstraint-2", "constraintType": ctype,
                "entityId": eid, "parameters": params}

    def coincident(self, point_a: str, point_b: str):
        self.constraints.append(self._con("COINCIDENT", self._id("co"),
            [self._str(point_a, "localFirst"), self._str(point_b, "localSecond")]))

    def horizontal(self, line: str):
        self.constraints.append(self._con("HORIZONTAL", self._id("h"), [self._str(line, "localFirst")]))

    def vertical(self, line: str):
        self.constraints.append(self._con("VERTICAL", self._id("v"), [self._str(line, "localFirst")]))

    def perpendicular(self, l1: str, l2: str):
        self.constraints.append(self._con("PERPENDICULAR", self._id("perp"),
            [self._str(l1, "localFirst"), self._str(l2, "localSecond")]))

    def parallel(self, l1: str, l2: str):
        self.constraints.append(self._con("PARALLEL", self._id("par"),
            [self._str(l1, "localFirst"), self._str(l2, "localSecond")]))

    def equal(self, e1: str, e2: str):
        self.constraints.append(self._con("EQUAL", self._id("eq"),
            [self._str(e1, "localFirst"), self._str(e2, "localSecond")]))

    def concentric(self, a: str, b: str):
        self.constraints.append(self._con("CONCENTRIC", self._id("conc"),
            [self._str(a, "localFirst"), self._str(b, "localSecond")]))

    def tangent(self, a: str, b: str):
        self.constraints.append(self._con("TANGENT", self._id("tan"),
            [self._str(a, "localFirst"), self._str(b, "localSecond")]))

    def midpoint(self, point: str, line: str):
        self.constraints.append(self._con("MIDPOINT", self._id("mid"),
            [self._str(point, "localFirst"), self._str(line, "localSecond")]))

    def symmetric(self, a: str, b: str, symmetry_line: str):
        self.constraints.append(self._con("SYMMETRIC", self._id("sym"),
            [self._str(a, "localFirst"), self._str(b, "localSecond"),
             self._str(symmetry_line, "local")]))

    # full documented binary set, routed generically
    _BINARY = {"coincident": "COINCIDENT", "parallel": "PARALLEL", "perpendicular": "PERPENDICULAR",
               "tangent": "TANGENT", "equal": "EQUAL", "concentric": "CONCENTRIC", "pierce": "PIERCE"}
    _UNARY = {"horizontal": "HORIZONTAL", "vertical": "VERTICAL", "fix": "FIX"}

    def constrain(self, kind: str, a: str, b: Optional[str] = None, c: Optional[str] = None):
        if kind in self._UNARY:
            self.constraints.append(self._con(self._UNARY[kind], self._id("u"), [self._str(a, "localFirst")]))
        elif kind in self._BINARY:
            self.constraints.append(self._con(self._BINARY[kind], self._id("b"),
                [self._str(a, "localFirst"), self._str(b, "localSecond")]))
        elif kind == "midpoint": self.midpoint(a, b)
        elif kind == "symmetric": self.symmetric(a, b, c)
        elif kind == "ground_origin": self.ground_origin(a)
        else: raise ValueError(f"unknown constraint kind {kind}")

    def ground_origin(self, point: str):
        """Coincident a sketch point to the part-studio origin — the idiomatic anchor."""
        self.constraints.append(self._con("COINCIDENT", self._id("ground"),
            [self._extq([ORIGIN_VERTEX], "externalFirst"), self._str(point, "localSecond")]))

    # ---- dimensions (driving) ---------------------------------------------
    def dim_length(self, line: str, value):
        self.constraints.append(self._con("LENGTH", self._id("dlen"), [
            self._str(line, "localFirst"),
            {"btType": "BTMParameterEnum-145", "value": "MINIMUM",
             "enumName": "DimensionDirection", "parameterId": "direction"},
            {"btType": "BTMParameterQuantity-147", "expression": _expr(value),
             "parameterId": "length", "isInteger": False},
            {"btType": "BTMParameterEnum-145", "value": "ALIGNED",
             "enumName": "DimensionAlignment", "parameterId": "alignment"}]))

    def dim_radius(self, circle: str, value):
        self.constraints.append(self._con("RADIUS", self._id("drad"), [
            self._str(f"{circle}.a", "localFirst"),
            {"btType": "BTMParameterQuantity-147", "expression": _expr(value),
             "parameterId": "radius", "isInteger": False}]))

    def dim_diameter(self, circle: str, value):
        self.constraints.append(self._con("DIAMETER", self._id("ddia"), [
            self._str(f"{circle}.a", "localFirst"),
            {"btType": "BTMParameterQuantity-147", "expression": _expr(value),
             "parameterId": "length", "isInteger": False}]))

    def dim_distance(self, a: str, b: str, value):
        self.constraints.append(self._con("DISTANCE", self._id("ddist"), [
            self._str(a, "localFirst"), self._str(b, "localSecond"),
            {"btType": "BTMParameterQuantity-147", "expression": _expr(value), "parameterId": "length"}]))

    def dim_angle(self, l1: str, l2: str, value):
        v = value if (isinstance(value, str)) else f"{value} deg"
        self.constraints.append(self._con("ANGLE", self._id("dang"), [
            self._str(l1, "localFirst"), self._str(l2, "localSecond"),
            {"btType": "BTMParameterQuantity-147", "expression": v, "parameterId": "angle"}]))

    def dim_distance_to_plane(self, plane: str, line: str, value):
        """DISTANCE from a default plane (external) to a sketch line — positions the sketch."""
        self.constraints.append(self._con("DISTANCE", self._id("ddist"), [
            self._extq([PLANES[plane]], "externalFirst"),
            self._str(line, "localSecond"),
            {"btType": "BTMParameterQuantity-147", "expression": _expr(value), "parameterId": "length"}]))

    # ---- emit --------------------------------------------------------------
    def build(self) -> Dict[str, Any]:
        return {"feature": {
            "btType": "BTMSketch-151", "featureType": "newSketch",
            "name": self.name, "suppressed": False,
            "parameters": [{"btType": "BTMParameterQueryList-148",
                            "queries": [{"btType": "BTMIndividualQuery-138",
                                         "deterministicIds": [PLANES[self.plane]]}],
                            "parameterId": "sketchPlane"}],
            "entities": self.entities, "constraints": self.constraints,
        }}
