"""配图员：题目通过检查后，按题干画配图。

模型不直接画图，只写一份"图纸"：各点坐标、要画的线、函数表达式或统计数据；
程序用 figlib 按图纸画出来：立体图按斜二测投影，被挡住的棱自动画成虚线；坐标图按真实比例描点。
画完再按坐标核对一遍图和题干是否一致（题干提到的点、中点、垂直、平行、长度），
对不上就把问题告诉配图员重画一次。
"""

from __future__ import annotations

import ast
import hashlib
import logging
import math
import re
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from mingti.figlib import Plot2D, Solid

logger = logging.getLogger("mingti.figures")

STAGE_DRAW = "figure-draw"
STAGE_REDRAW = "figure-redraw"

# 附在出题要求后面：需要配图的题，图里的信息都要在题干文字里写明。
# 审题、解题、复核都按文字核对，图在最后才画，所以题目不能靠图才能看懂。
FIGURE_RULE = (
    "如果这道题需要配图（立体几何、函数图象、圆锥曲线、统计图表等），可以用“如图”引出，"
    "但图中的全部信息（点的位置关系、长度、角度、函数图象经过的点、统计数据等）都要在题干文字里写明，"
    "做到不看图也能完整作答；配图会在题目通过检查后另外画出。"
)

_FIGURE_WORDS = re.compile(r"如图|下图|图中|所示的图|直方图|频率分布|条形图|示意图")
_SOLID_WORDS = re.compile(r"立体几何|空间向量|空间几何|棱柱|棱锥|棱台|正方体|长方体|四面体|圆柱|圆锥|圆台|二面角")


def needs_figure(draft: dict, brief: dict | None = None, meta: dict | None = None) -> bool:
    """题干说了"如图"、要求里写了"配图"，或者考的是立体几何，就配一张图。"""
    meta = meta or {}
    brief = brief or {}
    if meta.get("figure") == "off":
        return False
    if meta.get("figure") == "on":
        return True
    stem = draft.get("stem_latex") or draft.get("stem_plain") or ""
    if _FIGURE_WORDS.search(stem):
        return True
    asked = " ".join(str(part or "") for part in (brief.get("request_text"), meta.get("note")))
    if "配图" in asked:
        return True
    curriculum = draft.get("curriculum") or {}
    path = " ".join(str(curriculum.get(key) or "") for key in ("volume", "chapter", "section", "name"))
    return bool(_SOLID_WORDS.search(path))


class FigureError(ValueError):
    """图纸有问题，消息会原样告诉配图员让它改。"""


# ---------- 表达式：只允许四则运算、乘方和常用函数 ----------

_FUNCS = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "exp": math.exp, "log": math.log, "ln": math.log, "log10": math.log10, "log2": math.log2,
    "sqrt": math.sqrt, "abs": abs, "floor": math.floor, "ceil": math.ceil,
}
_CONSTS = {"pi": math.pi, "e": math.e}
_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Call, ast.Name, ast.Constant, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.USub, ast.UAdd,
)


def compile_expr(text: str, var: str):
    source = str(text or "").replace("^", "**").replace("π", "pi").strip()
    source = re.sub(r"^(?:y|f\s*\(\s*x\s*\))\s*=", "", source).strip()  # "y=2*sin(x)" 也认
    if not source or len(source) > 200:
        raise FigureError(f"表达式为空或太长：{source[:40]}")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise FigureError(f"表达式写法不对：{source}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _NODES):
            raise FigureError(f"表达式里有不支持的写法：{source}")
        if isinstance(node, ast.Name) and node.id not in _FUNCS and node.id not in _CONSTS and node.id != var:
            raise FigureError(f"表达式里有不认识的名字 {node.id}（自变量要写成 {var}）：{source}")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in _FUNCS):
            raise FigureError(f"表达式里有不支持的函数：{source}")
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
                raise FigureError(f"表达式里只能有数字：{source}")
            # 整数一律按浮点数算，免得 9**9**9 这样的大整数运算卡住
            node.value = float(node.value)
    code = compile(tree, "<figure>", "eval")
    env = {"__builtins__": {}, **_FUNCS, **_CONSTS}

    def fn(value):
        try:
            result = eval(code, env, {var: float(value)})  # noqa: S307 - 上面已逐个节点检查，只有算术表达式
        except (ArithmeticError, ValueError, TypeError):
            return math.nan
        # 负数开平方这类算出复数的地方当作没有定义，曲线在那里断开
        return math.nan if isinstance(result, complex) else float(result)

    return fn


# ---------- 图纸：检查并整理成固定格式 ----------

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9']{0,3}")


def _num(value, what) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise FigureError(f"{what}不是数") from exc
    if not math.isfinite(number) or abs(number) > 1e5:
        raise FigureError(f"{what}超出范围")
    return number


def _range(value, what) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise FigureError(f"{what}要写成 [最小值, 最大值]")
    low, high = _num(value[0], what), _num(value[1], what)
    if not low < high:
        raise FigureError(f"{what}的最小值要小于最大值")
    return [low, high]


_POINT_RUN = re.compile(r"[A-Za-z]\d*'*")


def _name_of(item, points):
    """把一个点的写法认成点名：标准写法是 "E"；模型有时写成 ["E"]、{"name": "E"}，
    或者直接写坐标 [1, 0, 1]，坐标和某个已知点重合时也认作那个点。认不出来返回 None。"""
    if isinstance(item, dict):
        item = item.get("name") or item.get("point")
    if isinstance(item, (list, tuple)):
        if len(item) == 1:
            return _name_of(item[0], points)
        if item and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in item):
            for name, xyz in points.items():
                if len(xyz) == len(item) and all(abs(a - float(b)) < 1e-6 for a, b in zip(xyz, item)):
                    return name
        return None
    return str(item).strip() if isinstance(item, str) else None


def _names(value, points, what) -> list[str]:
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, str):
        # "ABCD"、"A1B1C1" 这样连写的，拆成一个个点名
        value = [value] if value.strip() in points else _POINT_RUN.findall(value)
    names, unknown = [], []
    for item in value or []:
        name = _name_of(item, points)
        if name in points:
            names.append(name)
        else:
            unknown.append(str(item))
    if unknown:
        raise FigureError(
            f"{what} 里的 {'、'.join(unknown)} 不是 points 里的点；{what} 只写点名（如 \"E\"），点的坐标统一写在 points 里"
        )
    return names


def _pairs(value, points, what) -> list[list[str]]:
    pairs = []
    for item in value or []:
        names = _names(item, points, what)
        if len(names) != 2:
            raise FigureError(f"{what} 要写成 [点名, 点名] 的列表，比如 [\"P\", \"A\"]")
        pairs.append(names)
    return pairs


def _points(raw, dim) -> dict[str, list[float]]:
    if not isinstance(raw, dict):
        raise FigureError("points 要写成 {点名: 坐标}")
    points = {}
    for name, xy in raw.items():
        name = str(name)
        if not _NAME.fullmatch(name):
            raise FigureError(f"点名 {name} 不合规（用 A、B1、P 这样的写法）")
        if not isinstance(xy, (list, tuple)) or len(xy) != dim:
            raise FigureError(f"点 {name} 要给 {dim} 个坐标")
        points[name] = [_num(c, f"点 {name} 的坐标") for c in xy]
    return points


def normalize(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise FigureError("图纸要是一个 JSON 对象")
    kind = raw.get("kind")
    if kind == "none":
        return {"kind": "none"}
    if kind == "solid":
        return _normalize_solid(raw)
    if kind == "plane":
        return _normalize_plane(raw)
    if kind == "chart":
        return _normalize_chart(raw)
    raise FigureError("kind 只能是 solid、plane、chart 或 none")


def _normalize_solid(raw: dict) -> dict:
    points = _points(raw.get("points"), 3)
    if len(points) < 3:
        raise FigureError("立体图至少要给 3 个点的坐标")
    coords = np.array(list(points.values()))
    if float(np.max(coords.max(axis=0) - coords.min(axis=0))) < 1e-6:
        raise FigureError("所有点挤在一起了")
    faces = []
    for face in raw.get("faces") or []:
        names = _names(face, points, "faces")
        if len(names) < 3:
            raise FigureError("每个面至少要 3 个顶点")
        faces.append(names)
    circles = []
    for circle in raw.get("circles") or []:
        center = circle.get("center")
        center = points[center] if isinstance(center, str) and center in points else [_num(c, "圆心坐标") for c in (center or [])]
        if len(center) != 3:
            raise FigureError("立体图里圆的 center 要给点名或 [x, y, z]")
        plane = circle.get("plane", "xy")
        if plane not in ("xy", "xz", "yz"):
            raise FigureError("圆所在的平面 plane 只能是 xy、xz 或 yz")
        circles.append({"center": center, "radius": abs(_num(circle.get("radius"), "圆的半径")), "plane": plane})
    angle = raw.get("angle", 45)
    return {
        "kind": "solid",
        "points": points,
        "faces": faces,
        # 立体图里哪段画虚线完全按遮挡来定（程序自动算），配图员放进 dashed 的线也一样处理；
        # 不然同一条线既按实线又按虚线画两遍，叠在一起看着像实线
        "segments": list({tuple(sorted(pair)): pair for pair in
                          _pairs(raw.get("segments"), points, "segments") + _pairs(raw.get("dashed"), points, "dashed")}.values()),
        "dashed": [],
        "dots": _names(raw.get("dots"), points, "dots"),
        "labels": _names(raw.get("labels") or list(points), points, "labels"),
        "circles": circles,
        "angle": min(160.0, max(20.0, _num(angle, "angle"))),
    }


_EXPR_KEYS = ("expr", "y", "f", "fx", "expression", "function")


def _function_expr(item) -> str:
    """函数项里的表达式。标准写法 {"expr": "2*sin(2*x)"}；也认直接写字符串、写在 y 下面，
    以及只有一个 x 键的写法（模型偶尔把 y=f(x) 的表达式写在 x 下面）。"""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in _EXPR_KEYS:
            if isinstance(item.get(key), str) and item[key].strip():
                return item[key]
        if isinstance(item.get("x"), str) and "y" not in item:
            return item["x"]
    raise FigureError('functions 的每一项写成 {"expr": "2*sin(2*x)", "from": 0, "to": 6.28}，表达式写在 expr 里')


def _text_item(item, points) -> dict:
    """文字说明。标准写法 {"at": [x, y], "text": "..."}，at 也可以是点名；也认 {"x":, "y":, "text":} 和 [x, y, 文字]。"""
    if isinstance(item, (list, tuple)) and len(item) == 3:
        item = {"at": list(item[:2]), "text": item[2]}
    if not isinstance(item, dict):
        raise FigureError('texts 的每一项写成 {"at": [x, y], "text": "文字"}')
    at = item.get("at", item.get("position"))
    if at is None and "x" in item and "y" in item:
        at = [item["x"], item["y"]]
    at = points[at] if isinstance(at, str) and at in points else [_num(c, "文字位置") for c in (at or [])]
    if len(at) != 2:
        raise FigureError('texts 的每一项写成 {"at": [x, y], "text": "文字"}，at 也可以写点名')
    return {"at": at, "text": str(item.get("text") or "")[:16]}


def _normalize_plane(raw: dict) -> dict:
    points = _points(raw.get("points") or {}, 2)
    x_range = _range(raw.get("x_range"), "x_range")
    y_range = _range(raw.get("y_range"), "y_range")
    if max(x_range[1] - x_range[0], y_range[1] - y_range[0]) > 2000:
        raise FigureError("显示范围太大")
    functions = []
    for item in (raw.get("functions") or [])[:6]:
        expr = _function_expr(item)
        compile_expr(expr, "x")
        item = item if isinstance(item, dict) else {}
        low = _num(item.get("from", x_range[0]), "函数的定义域")
        high = _num(item.get("to", x_range[1]), "函数的定义域")
        functions.append({"expr": expr, "from": low, "to": high, "dashed": bool(item.get("dashed"))})
    curves = []
    for item in (raw.get("param_curves") or [])[:6]:
        if not isinstance(item, dict):
            raise FigureError('param_curves 的每一项写成 {"x": "3*cos(t)", "y": "2*sin(t)", "from": 0, "to": 6.2832}')
        compile_expr(item.get("x"), "t")
        compile_expr(item.get("y"), "t")
        curves.append({
            "x": str(item["x"]), "y": str(item["y"]),
            "from": _num(item.get("from", 0), "参数范围"), "to": _num(item.get("to", 2 * math.pi), "参数范围"),
            "dashed": bool(item.get("dashed")),
        })
    circles = []
    for item in (raw.get("circles") or [])[:6]:
        center = item.get("center")
        center = points[center] if isinstance(center, str) and center in points else [_num(c, "圆心坐标") for c in (center or [])]
        if len(center) != 2:
            raise FigureError("圆的 center 要给点名或 [x, y]")
        circles.append({"center": center, "radius": abs(_num(item.get("radius"), "圆的半径")), "dashed": bool(item.get("dashed"))})
    marks = []
    for item in (raw.get("marks") or [])[:16]:
        if not isinstance(item, dict):
            raise FigureError('marks 的每一项写成 {"axis": "x", "value": 0.5236, "text": "π/6"}')
        axis, value = item.get("axis"), item.get("value")
        if axis is None and ("x" in item) != ("y" in item):  # {"x": 0.52, "text": "π/6"} 这种写法
            axis = "x" if "x" in item else "y"
            value = item[axis]
        if axis not in ("x", "y"):
            raise FigureError('marks 的 axis 只能是 "x" 或 "y"，写成 {"axis": "x", "value": 0.5236, "text": "π/6"}')
        marks.append({"axis": axis, "value": _num(value, "刻度"), "text": str(item.get("text") or "")[:12]})
    texts = [_text_item(item, points) for item in (raw.get("texts") or [])[:10]]
    polygons = []
    for polygon in raw.get("polygons") or []:
        names = _names(polygon, points, "polygons")
        if len(names) < 3:
            raise FigureError("多边形至少要 3 个顶点")
        polygons.append(names)
    has_curves = bool(functions or curves)
    labels = _names(raw.get("labels") if raw.get("labels") is not None else list(points), points, "labels")
    # 配图员有时把点名在 texts 里又写一遍，图上就成了"MM""PP"：点名已经标了的，texts 里的同名文字去掉
    texts = [item for item in texts if item["text"].strip() not in labels]
    return {
        "kind": "plane",
        "x_range": x_range,
        "y_range": y_range,
        "axes": bool(raw.get("axes", has_curves)),
        "points": points,
        "functions": functions,
        "param_curves": curves,
        "circles": circles,
        "polygons": polygons,
        "segments": _pairs(raw.get("segments"), points, "segments"),
        "dashed": _pairs(raw.get("dashed"), points, "dashed"),
        "dots": _names(raw.get("dots") if raw.get("dots") is not None else list(points), points, "dots"),
        "labels": labels,
        "marks": marks,
        "texts": texts,
    }


def _normalize_chart(raw: dict) -> dict:
    chart = raw.get("chart") if isinstance(raw.get("chart"), dict) else raw
    kind = chart.get("type", "histogram")
    if kind == "histogram":
        y_label = str(chart.get("y_label") or "频率/组距")[:16]
        usage = '每一组写成 [左端点, 右端点, 高度]；要隐藏某一组的高度，高度照常写数值，标注写在第 4 项，如 [90, 100, 0.01, "a"]'
        bins, unknown = [], []
        for item in (chart.get("bins") or [])[:24]:
            if not isinstance(item, (list, tuple)) or len(item) not in (3, 4):
                raise FigureError(f"直方图的 bins {usage}")
            left, right = (_num(v, f"直方图分组 {item} 的端点") for v in item[:2])
            # 第 4 项是纵轴上这一组高度的标注：不写就标数值，写字母（如 a）就标字母，空字符串就不标
            tick = None if len(item) == 3 or item[3] is None else str(item[3]).strip()[:6]
            try:
                height = _num(item[2], "")
            except FigureError:
                # 高度写成了字母（待求的那一组），先记下来，后面按各组频率之和为 1 算出来
                if not isinstance(item[2], str) or len(item) == 4:
                    raise FigureError(f"直方图分组 {item} 的高度不是数；{usage}") from None
                height, tick = None, item[2].strip()[:6]
                unknown.append(len(bins))
            if not left < right or (height is not None and height < 0):
                raise FigureError("直方图每组的左端点要小于右端点，高度不能是负数")
            bins.append([left, right, height, tick])
        if not bins:
            raise FigureError("直方图没有数据")
        if unknown:
            known = sum(h * (r - l) for l, r, h, _ in bins if h is not None)
            low, high, _, _ = bins[unknown[0]]
            if len(unknown) > 1 or "频率" not in y_label or not 0 < 1 - known <= 1:
                raise FigureError(f"直方图有组的高度不是数；{usage}")
            bins[unknown[0]][2] = round((1 - known) / (high - low), 10)
        bins.sort(key=lambda b: (b[0], b[1]))
        return {"kind": "chart", "type": "histogram", "bins": bins,
                "x_label": str(chart.get("x_label") or "")[:16], "y_label": y_label}
    if kind == "bar":
        categories = []
        for item in (chart.get("categories") or [])[:14]:
            if not isinstance(item, (list, tuple)) or len(item) not in (2, 3):
                raise FigureError("条形图的 categories 要写成 [名称, 数值] 或 [名称, 数值, 顶上的标注] 的列表")
            value = _num(item[1], "条形图的数据")
            if value < 0:
                raise FigureError("条形图的数值不能是负数")
            tick = None if len(item) == 2 or item[2] is None else str(item[2]).strip()[:6]
            categories.append([str(item[0])[:8], value, tick])
        if not categories:
            raise FigureError("条形图没有数据")
        return {"kind": "chart", "type": "bar", "categories": categories,
                "x_label": str(chart.get("x_label") or "")[:16], "y_label": str(chart.get("y_label") or "")[:16]}
    raise FigureError("chart.type 只能是 histogram 或 bar")


# ---------- 按图纸画图 ----------


def _fmt(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _text_px(text: str) -> float:
    """文字大约多宽（15 号字：汉字约 15 像素，数字和字母约 8 像素），用来给标注留出位置。"""
    return sum(15 if ord(ch) > 0x2E80 else 8 for ch in text)


def _circle_points(center, radius, plane, n=96):
    c = np.array(center, float)
    out = []
    for i in range(n + 1):
        t = 2 * math.pi * i / n
        a, b = radius * math.cos(t), radius * math.sin(t)
        offset = {"xy": (a, b, 0.0), "xz": (a, 0.0, b), "yz": (0.0, a, b)}[plane]
        out.append(c + np.array(offset))
    return out


def render(spec: dict) -> str:
    if spec["kind"] == "solid":
        return _render_solid(spec)
    if spec["kind"] == "plane":
        return _render_plane(spec)
    if spec["kind"] == "chart":
        return _render_chart(spec)
    raise FigureError("这张图纸不用画")


def _clearance(spec: dict, angle: float, k: float = 0.5) -> float:
    """按这个倾角投影后，点离"不经过它的线段"、点离点最近有多远。
    太近就是投影重合了：比如某个点正好落在另一条线上，看起来像在线上，或者两条线叠在一起。"""
    a = math.radians(angle)
    points = {n: np.array(p, float) for n, p in spec["points"].items()}
    flat = {n: np.array([p[0] + k * p[1] * math.cos(a), p[2] + k * p[1] * math.sin(a)]) for n, p in points.items()}
    lines = {tuple(sorted(pair)) for pair in spec["segments"] + spec["dashed"]}
    for face in spec["faces"]:
        lines.update(tuple(sorted(pair)) for pair in zip(face, face[1:] + face[:1]))

    def gap(p, a, b):
        ab = b - a
        t = 0.0 if not ab @ ab else max(0.0, min(1.0, float((p - a) @ ab / (ab @ ab))))
        return float(np.linalg.norm(p - a - t * ab))

    best = math.inf
    names = list(points)
    for i, n in enumerate(names):
        for m in names[i + 1:]:
            if np.linalg.norm(points[n] - points[m]) > 1e-6:
                best = min(best, float(np.linalg.norm(flat[n] - flat[m])))
        for a_name, b_name in lines:
            if n in (a_name, b_name) or gap(points[n], points[a_name], points[b_name]) < 1e-6:
                continue  # 点本来就在这条线上（比如中点），投影后在线上是对的
            best = min(best, gap(flat[n], flat[a_name], flat[b_name]))
    return best


def _pick_angle(spec: dict, span: float) -> float:
    """配图员给的倾角投影后有点和线重合时，换一个最不重合的倾角。"""
    enough = 0.06 * span
    candidates = [spec["angle"], 45, 35, 55, 30, 60, 40, 50, 25, 65, 135]
    for angle in candidates:
        if _clearance(spec, angle) >= enough:
            return angle
    return max(candidates, key=lambda angle: _clearance(spec, angle))


def _render_solid(spec: dict) -> str:
    coords = np.array(list(spec["points"].values()))
    span = float(np.max(coords.max(axis=0) - coords.min(axis=0)))
    angle = _pick_angle(spec, span)
    solid = Solid(spec["points"], spec["faces"], k=0.5, ang=angle, scale=max(18.0, min(70.0, 190.0 / span)))
    for a, b in spec["segments"]:
        solid.seg(a, b)
    for a, b in spec["dashed"]:
        solid.seg(a, b, "dash")
    for circle in spec["circles"]:
        ring = _circle_points(circle["center"], circle["radius"], circle["plane"])
        for p, q in zip(ring, ring[1:]):
            solid.seg(p, q, n=6)
    for name in spec["dots"]:
        solid.dot(name)
    for name in spec["labels"]:
        solid.label(name)
    return solid.svg()


def _render_plane(spec: dict) -> str:
    (x0, x1), (y0, y1) = spec["x_range"], spec["y_range"]
    ux = uy = max(10.0, min(80.0, 280.0 / max(x1 - x0, y1 - y0)))
    # 圆、多边形、圆锥曲线要按真实比例画，不然会变形。只有函数图象时，按同一比例画出来太窄太高
    # （比如周期很短的三角函数）或太扁，就横纵轴各定比例，像教材里的三角函数图那样拉开
    shapes = spec["circles"] or spec["polygons"] or spec["param_curves"]
    width, height = (x1 - x0) * ux, (y1 - y0) * uy
    if not shapes and (height > 1.4 * width or width > 3.2 * height):
        ux = max(10.0, min(400.0, 300.0 / (x1 - x0)))
        uy = max(10.0, min(400.0, 190.0 / (y1 - y0)))
    # y 轴上的刻度文字写在轴的左边，显示范围不够宽就往左让一点，免得被裁掉
    y_marks = [m for m in spec["marks"] if m["axis"] == "y"]
    if spec["axes"] and y_marks:
        x0 = min(x0, -(max(_text_px(m["text"]) for m in y_marks) + 12) / ux)
    # 点和文字不要贴着边：每个点四周留出点名的位置（右边、上边还要让开坐标轴的箭头），文字整段都放得下。
    # 以前点落在显示范围的边上时，点名和 x 轴箭头挤在一起，靠边的文字被裁掉一截
    for px, py in spec["points"].values():
        x0, x1 = min(x0, px - 24 / ux), max(x1, px + 30 / ux)
        y0, y1 = min(y0, py - 24 / uy), max(y1, py + 26 / uy)
    for item in spec["texts"]:
        tx, ty = item["at"][0], item["at"][1]
        x1 = max(x1, tx + (_text_px(item["text"]) + 10) / ux)
        y1 = max(y1, ty + 18 / uy)
    plot = Plot2D((x0, x1), (y0, y1), unit=ux, uy=uy, axes=spec["axes"])
    for item in spec["functions"]:
        plot.curve(compile_expr(item["expr"], "x"), max(item["from"], x0), min(item["to"], x1), cls="dash" if item["dashed"] else "ln")
    for item in spec["param_curves"]:
        fx, fy = compile_expr(item["x"], "t"), compile_expr(item["y"], "t")
        plot.curve(lambda t, fx=fx, fy=fy: (fx(t), fy(t)), item["from"], item["to"], param=True, cls="dash" if item["dashed"] else "ln")
    for item in spec["circles"]:
        (cx, cy), r = item["center"], item["radius"]
        plot.curve(lambda t, cx=cx, cy=cy, r=r: (cx + r * math.cos(t), cy + r * math.sin(t)), 0, 2 * math.pi,
                   param=True, cls="dash" if item["dashed"] else "ln")
    points = spec["points"]
    for polygon in spec["polygons"]:
        plot.poly([points[n] for n in polygon], close=True)
    for a, b in spec["segments"]:
        plot.line(points[a], points[b])
    for a, b in spec["dashed"]:
        plot.line(points[a], points[b], cls="dash")
    for mark in spec["marks"]:
        if mark["axis"] == "x":
            plot.line((mark["value"], 0), (mark["value"], 4 / plot.uy), cls="thin")
            plot.text(mark["value"], 0, mark["text"], -4 * len(mark["text"]), 16, upright=True)
        else:
            plot.line((0, mark["value"]), (4 / plot.ux, mark["value"]), cls="thin")
            plot.text(0, mark["value"], mark["text"], -8 * len(mark["text"]) - 6, 5, upright=True)
    for name in spec["dots"]:
        plot.dot(points[name])
    # 点名放在"图形中心 → 该点"的外侧，免得压在线上
    if points:
        cx = sum(p[0] for p in points.values()) / len(points)
        cy = sum(p[1] for p in points.values()) / len(points)
        for name in spec["labels"]:
            px, py = points[name]
            if spec["axes"] and name == "O" and abs(px) < 1e-9 and abs(py) < 1e-9:
                continue  # 坐标轴已经在原点写了 O，再标一次就成了"OO"
            vx, vy = (px - cx) * plot.ux, (cy - py) * plot.uy
            norm = math.hypot(vx, vy) or 1.0
            dx, dy = (10 * vx / norm - 5, 10 * vy / norm + 5) if math.hypot(vx, vy) > 1 else (6, -6)
            plot.text(px, py, name, dx, dy)
    for item in spec["texts"]:
        plot.text(item["at"][0], item["at"][1], item["text"], 4, -4, upright=True)
    return plot.svg()


def _render_chart(spec: dict) -> str:
    if spec["type"] == "histogram":
        bins = spec["bins"]
        left, right = bins[0][0], bins[-1][1]
        top = max(b[2] for b in bins) or 1.0
        span = right - left
        ux, uy = 300 / (span * 1.2), 170 / (top * 1.42)
        # 纵轴上每个高度标什么：默认标数值；图纸给了标注就用标注（字母或不标）
        ticks: dict[float, str] = {}
        for _, _, height, tick in bins:
            if height > 0 and height not in ticks:
                ticks[height] = _fmt(height) if tick is None else tick
        axis_x = left - span * 0.08
        # 左边留出纵轴刻度文字的宽度，右边留出横轴名称的宽度
        pad_left = (max((_text_px(text) for text in ticks.values()), default=0) + 14) / ux
        axis_end = right + span * 0.1
        pad_right = (_text_px(spec["x_label"]) + 8) / ux
        plot = Plot2D((axis_x - pad_left, axis_end + pad_right), (-top * 0.12, top * 1.3), unit=ux, uy=uy, axes=False)
        plot.arrow_line((axis_x, 0), (axis_end, 0))
        plot.arrow_line((axis_x, 0), (axis_x, top * 1.26))
        plot.text(axis_x, 0, "O", -13, 14)
        for low, high, height, _ in bins:
            plot.poly([(low, 0), (low, height), (high, height), (high, 0)], close=True)
        for edge in sorted({b[0] for b in bins} | {b[1] for b in bins}):
            plot.text(edge, 0, _fmt(edge), -_text_px(_fmt(edge)) / 2, 16, upright=True)
        for height, text in sorted(ticks.items()):
            if not text:
                continue
            first = min(b[0] for b in bins if b[2] == height)
            plot.line((axis_x, height), (first, height), cls="dash")
            plot.text(axis_x, height, text, -_text_px(text) - 5, 5, upright=not re.fullmatch(r"[A-Za-z]", text))
        if spec["x_label"]:
            plot.text(axis_end, 0, spec["x_label"], 4, 5, upright=True)
        plot.text(axis_x, top * 1.26, spec["y_label"], 6, 4, upright=True)
        return plot.svg()
    categories = spec["categories"]
    top = max(c[1] for c in categories) or 1.0
    count = len(categories)
    plot = Plot2D((-0.9, count + 0.6), (-top * 0.14, top * 1.3), unit=300 / (count + 1.5), uy=170 / (top * 1.44), axes=False)
    plot.arrow_line((-0.5, 0), (count + 0.4, 0))
    plot.arrow_line((-0.5, 0), (-0.5, top * 1.26))
    for i, (name, value, tick) in enumerate(categories):
        plot.poly([(i, 0), (i, value), (i + 0.6, value), (i + 0.6, 0)], close=True)
        plot.text(i + 0.3, 0, name, -6 * len(name), 16, upright=True)
        text = _fmt(value) if tick is None else tick
        if text:
            plot.text(i + 0.3, value, text, -4 * len(text), -5, upright=True)
    if spec["y_label"]:
        plot.text(-0.5, top * 1.26, spec["y_label"], 6, 4, upright=True)
    if spec["x_label"]:
        plot.text(count + 0.4, 0, spec["x_label"], -6 * len(spec["x_label"]), -8, upright=True)
    return plot.svg()


# ---------- 核对：图和题干对得上吗 ----------

_P = r"[A-Z]\d?'?"


def plain_stem(stem: str) -> str:
    """把题干里的 LaTeX 化成便于匹配的纯文字：A_{1} → A1，\\perp → ⊥，\\sqrt{2} → √2，去掉空格和 $。"""
    s = str(stem or "")
    s = re.sub(r"\\(?:mathrm|text|mathit|operatorname|boldsymbol|overline|vec)\s*\{([^{}]*)\}", r"\1", s)
    for old, new in ((r"\perp", "⊥"), (r"\bot", "⊥"), (r"\parallel", "∥"), ("//", "∥"),
                     (r"\angle", "∠"), (r"\triangle", "△"), (r"\cdot", "·"), (r"\left", ""), (r"\right", ""),
                     ("，", ","), ("、", ","), ("；", ";")):
        s = s.replace(old, new)
    s = re.sub(r"\\sqrt\s*\{(\d+)\}", r"√\1", s)
    s = re.sub(r"\\sqrt\s*(\d)", r"√\1", s)
    s = re.sub(r"\^\s*\{?\s*\\prime\s*\}?|\^\s*\{?\s*'\s*\}?", "'", s)
    s = re.sub(r"_\s*\{?\s*(\d+)\s*\}?", r"\1", s)
    s = s.replace("$", "").replace("\\(", "").replace("\\)", "")
    return re.sub(r"[{}\s]", "", s)


def _tokens(run: str) -> list[str]:
    return re.findall(_P, run)


def _curve_samples(spec: dict) -> list[np.ndarray]:
    """平面图里的每条曲线（参数曲线、圆、函数图象）按顺序取的点，用来判断点在不在曲线上、线是不是切线。"""
    curves = []
    for item in spec["param_curves"]:
        fx, fy = compile_expr(item["x"], "t"), compile_expr(item["y"], "t")
        ts = np.linspace(item["from"], item["to"], 1441)
        curves.append(np.array([[fx(t), fy(t)] for t in ts], float))
    for circle in spec["circles"]:
        ts = np.linspace(0, 2 * math.pi, 1441)
        cx, cy = circle["center"][:2]
        curves.append(np.column_stack([cx + circle["radius"] * np.cos(ts), cy + circle["radius"] * np.sin(ts)]))
    for item in spec["functions"]:
        fy = compile_expr(item["expr"], "x")
        xs = np.linspace(item["from"], item["to"], 1441)
        curves.append(np.array([[x, fy(x)] for x in xs], float))
    return [curve[np.all(np.isfinite(curve), axis=1)] for curve in curves if len(curve)]


def _check_curves(text: str, spec: dict) -> list[str]:
    """坐标图里和曲线有关的关系：题干说的切点、曲线上的点要在曲线上；从曲线外一点连到切点的线要和曲线相切。
    以前坐标图一律不核对，切线画歪了也查不出来（椭圆的两条切线、切点弦都画错过）。"""
    points = {name: np.array(xy, float)[:2] for name, xy in spec["points"].items()}
    curves = [curve for curve in _curve_samples(spec) if len(curve) > 2]
    if not points or not curves:
        return []
    span = max(spec["x_range"][1] - spec["x_range"][0], spec["y_range"][1] - spec["y_range"][0])
    near = 0.015 * span

    def nearest(point):
        """离这个点最近的曲线：(距离, 那里的切线方向)。"""
        best = (math.inf, None)
        for curve in curves:
            gaps = np.linalg.norm(curve - point, axis=1)
            i = int(np.argmin(gaps))
            if gaps[i] < best[0]:
                a, b = curve[max(i - 2, 0)], curve[min(i + 2, len(curve) - 1)]
                best = (float(gaps[i]), b - a)
        return best

    names = lambda run: [name for name in _tokens(run) if name in points]  # noqa: E731
    touching = []
    for run in re.findall(rf"切点(?:分别)?(?:为|是)((?:{_P},?)+)", text):
        touching += names(run)
    on_curve = list(touching)
    for run in re.findall(rf"((?:{_P},?)+)(?:两点)?(?:都|均)?(?:是|为|在)(?:椭圆|双曲线|抛物线|圆|曲线)[A-Z\d]*上", text):
        on_curve += names(run)
    for run in re.findall(rf"交(?:椭圆|双曲线|抛物线|圆|曲线)[A-Z\d]*于((?:点)?(?:{_P},?)+)", text):
        on_curve += names(run)
    problems = []
    for name in dict.fromkeys(on_curve):
        if nearest(points[name])[0] > near:
            problems.append(f"题干说 {name} 在曲线上，图里 {name} 不在曲线上")
    for name in dict.fromkeys(touching):
        gap, tangent = nearest(points[name])
        if gap > near or tangent is None or not np.linalg.norm(tangent):
            continue
        for a, b in spec["segments"]:
            other = b if a == name else a if b == name else None
            if other is None or other not in points or nearest(points[other])[0] <= near:
                continue  # 另一端也在曲线上的是弦，不是切线
            line = points[other] - points[name]
            cross = float(line[0] * tangent[1] - line[1] * tangent[0])
            sine = abs(cross) / (np.linalg.norm(line) * np.linalg.norm(tangent) + 1e-12)
            if sine > 0.06:  # 夹角超过约 3.4°
                problems.append(f"题干说 {name} 是切点，图里 {other}{name} 在 {name} 处和曲线不相切，切点要按题设算准")
    return list(dict.fromkeys(problems))


def check(stem: str, spec: dict) -> list[str]:
    """返回图和题干对不上的地方；没有问题返回空列表。只核对能从坐标算出来的关系。"""
    if spec["kind"] not in ("solid", "plane"):
        return []
    points = {name: np.array(xyz, float) for name, xyz in spec["points"].items()}
    if spec["kind"] == "plane" and (spec["functions"] or spec["param_curves"]) and not spec["polygons"]:
        # 函数图象、圆锥曲线类的题，字母多半不是线段名，不按几何关系核对；只核对和曲线有关的切点、曲线上的点
        return _check_curves(plain_stem(stem), spec)
    text = plain_stem(stem)
    problems: list[str] = []
    if points:
        coords = np.array(list(points.values()))
        size = float(np.max(coords.max(axis=0) - coords.min(axis=0))) or 1.0
    else:
        size = 1.0

    # 题干里以线段、平面名出现的点（至少两个字母连写），图里都要有
    named = set()
    for run in re.findall(rf"(?:{_P}){{2,}}", text):
        named.update(_tokens(run))
    missing = sorted(named - set(points))
    if missing:
        problems.append(f"题干里提到的点 {'、'.join(missing)} 图里没有")

    def vec(a, b):
        return points[b] - points[a]

    def have(*names):
        return all(n in points for n in names)

    def perpendicular(u, v):
        return abs(float(np.dot(u, v))) <= 0.03 * (np.linalg.norm(u) * np.linalg.norm(v) + 1e-12)

    def parallel(u, v):
        cross = np.cross(np.append(u, 0)[:3] if len(u) == 2 else u, np.append(v, 0)[:3] if len(v) == 2 else v)
        return float(np.linalg.norm(cross)) <= 0.03 * (np.linalg.norm(u) * np.linalg.norm(v) + 1e-12)

    # 中点：E 为 PC 的中点；E,F 分别为 PB,PC 的中点
    for m, a, b in re.findall(rf"({_P})(?:为|是)(?:棱|边|线段|弦|对角线)?({_P})({_P})的?中点", text):
        if have(m, a, b) and np.linalg.norm(points[m] - (points[a] + points[b]) / 2) > 0.02 * size:
            problems.append(f"题干说 {m} 是 {a}{b} 的中点，图里 {m} 不在 {a}{b} 中点")
    for m1, m2, a1, b1, a2, b2 in re.findall(
        rf"({_P}),({_P})分别(?:为|是)(?:棱|边|线段)?({_P})({_P}),(?:棱|边|线段)?({_P})({_P})的?中点", text
    ):
        for m, a, b in ((m1, a1, b1), (m2, a2, b2)):
            if have(m, a, b) and np.linalg.norm(points[m] - (points[a] + points[b]) / 2) > 0.02 * size:
                problems.append(f"题干说 {m} 是 {a}{b} 的中点，图里 {m} 不在 {a}{b} 中点")

    # 线面垂直：PA⊥平面ABCD
    for a, b, plane in re.findall(rf"({_P})({_P})⊥(?:平面|底面|面)((?:{_P}){{3,}})", text):
        corners = _tokens(plane)[:3]
        if have(a, b, *corners) and points[a].shape[0] == 3:
            u = vec(a, b)
            if not (perpendicular(u, vec(corners[0], corners[1])) and perpendicular(u, vec(corners[0], corners[2]))):
                problems.append(f"题干说 {a}{b}⊥平面{plane}，图里不垂直")

    # 线线垂直、平行：AB⊥CD，AB∥CD（后面不能紧跟字母，免得把平面名截断）
    for a, b, c, d in re.findall(rf"({_P})({_P})⊥({_P})({_P})(?![A-Z\d'])", text):
        if have(a, b, c, d) and not perpendicular(vec(a, b), vec(c, d)):
            problems.append(f"题干说 {a}{b}⊥{c}{d}，图里不垂直")
    for a, b, c, d in re.findall(rf"({_P})({_P})∥({_P})({_P})(?![A-Z\d'])", text):
        if have(a, b, c, d) and not parallel(vec(a, b), vec(c, d)):
            problems.append(f"题干说 {a}{b}∥{c}{d}，图里不平行")

    # 长度：AB=2，AB=2√2，PA=AB，AB=2AD
    for a, b, k, root, c, d in re.findall(
        rf"(?<![A-Z\d'])({_P})({_P})=(\d+(?:\.\d+)?)?(?:√(\d+))?(?:({_P})({_P}))?(?![A-Z\d'.√])", text
    ):
        if not have(a, b):
            continue
        length = float(np.linalg.norm(vec(a, b)))
        if c and d:
            if not have(c, d):
                continue
            target = (float(k) if k else 1.0) * float(np.linalg.norm(vec(c, d)))
            said = f"{a}{b}={k}{c}{d}"
        elif k or root:
            target = (float(k) if k else 1.0) * (math.sqrt(float(root)) if root else 1.0)
            said = f"{a}{b}={k}{'√' + root if root else ''}"
        else:
            continue
        if abs(length - target) > 0.02 * max(1.0, target):
            problems.append(f"题干说 {said}，图里 {a}{b} 的长度是 {length:.3g}")
    return list(dict.fromkeys(problems))


def answer_numbers(answer: str) -> list[float]:
    """答案里出现的数（去掉"（1）"这类小问编号；分数、百分数都折成小数）。"""
    text = re.sub(r"[（(]\s*\d+\s*[）)]", " ", str(answer or ""))
    text = re.sub(r"\\[dt]?frac\s*\{(-?\d+(?:\.\d+)?)\}\s*\{(\d+(?:\.\d+)?)\}", r" \1/\2 ", text)
    values = []
    for whole, below, percent in re.findall(r"(-?\d+(?:\.\d+)?)(?:\s*/\s*(\d+(?:\.\d+)?))?(\s*%)?", text):
        value = float(whole) / float(below) if below and float(below) else float(whole)
        values.append(value / 100 if percent else value)
    return [v for v in values if v != 0]


def _coordinate_pairs(text: str) -> set[str]:
    """文字里的坐标，如 (1,0)、(-2,√3)，统一成没有空格的写法。"""
    plain = plain_stem(text).replace("（", "(").replace("）", ")")
    return set(re.findall(r"\(-?[\d.√/]+,-?[\d.√/]+\)", plain))


def reveals_answer(spec: dict, answer: str, stem: str = "") -> list[str]:
    """图里把题目要求的结果直接标出来了，返回问题：统计图里要求的那一组的高或频率；
    坐标图里要求的点的坐标（比如"恒过定点"问的那个定点，图上写着 (1,0)）。题干里已经给出的不算。"""
    if spec.get("kind") == "plane":
        asked = _coordinate_pairs(answer) - _coordinate_pairs(stem)
        return [
            f"图里写了“{item['text']}”，这是题目要求的结果，不能标在图上；只标点名，不写坐标"
            for item in spec["texts"]
            if _coordinate_pairs(item["text"]) & asked
        ]
    if spec.get("kind") != "chart":
        return []
    wanted = answer_numbers(answer)
    hit = lambda value: any(abs(value - a) <= 1e-9 + 1e-6 * abs(a) for a in wanted)  # noqa: E731
    problems = []
    if spec["type"] == "histogram":
        for low, high, height, tick in spec["bins"]:
            if tick is None and height > 0 and (hit(height) or hit(height * (high - low))):
                problems.append(
                    f"[{_fmt(low)},{_fmt(high)}) 这一组的高 {_fmt(height)} 标在了纵轴上，它（或这一组的频率）正是题目要求的答案；"
                    "这一组的纵轴标注请写成字母（如 a）或空字符串"
                )
    else:
        for name, value, tick in spec["categories"]:
            if tick is None and hit(value):
                problems.append(f"“{name}”的数值 {_fmt(value)} 正是题目要求的答案；它顶上的标注请写成字母或空字符串")
    return problems


# ---------- 配图员 ----------

INSTRUCTIONS = (
    "你是高中数学试卷的配图员。根据题干（必要时参考解析）给这道题配一张图。你不直接画图，只写“图纸”，"
    "程序会按图纸画出教材风格的图。\n"
    "先定 kind：solid 立体几何图（棱柱、棱锥、棱台、正方体、四面体、圆柱、圆锥等）；"
    "plane 平面图（坐标系里的函数图象、圆锥曲线，或不带坐标轴的平面几何图、实际情境示意图）；"
    "chart 统计图表（频率分布直方图、条形图）；none 这道题不需要配图。\n"
    "要求：\n"
    "1. 点的位置、长度、角度、位置关系必须和题干完全一致：中点就取两端点坐标的平均，垂直就让向量点积为 0，长度按题干给的数。"
    "题干没给具体长度时，取看起来匀称的数值。曲线上的点要真的在曲线上；切线的切点要按题设算出来"
    "（比如先给参数取一个具体值，再用切线方程或切点弦方程求切点坐标），不能凭感觉取，程序会核对是否相切。\n"
    "2. solid：points 给每个点的三维坐标 [x, y, z]，z 轴竖直向上，y 轴是纵深（指向纸内），底面放在 z=0 上。"
    "faces 列出几何体的所有面（每个面按顶点顺序列点名），程序靠它判断哪些线被挡住、自动画成虚线；"
    "segments 列出要画的全部棱和题干提到的辅助线（对角线、连线、高等），每条线只写一次，"
    "实线还是虚线由程序按遮挡决定，立体图不用写 dashed。dots 列出棱上、面上需要加点标出的点（如中点）。"
    "circles 可以画圆柱、圆锥的底面：{\"center\": 点名或坐标, \"radius\": 半径, \"plane\": \"xy\"}。"
    "angle 是纵深轴的倾角，默认 45。\n"
    "3. plane：x_range、y_range 给显示范围，把要画的东西都包进去并留出空白。axes 表示画不画坐标轴（函数、圆锥曲线画，纯几何图不画）。"
    "functions 写 y 关于 x 的表达式（Python 写法，如 \"2*sin(2*x+pi/6)\"、\"x**3-3*x\"、\"log(x)\"），可给 from、to 限定定义域；"
    "圆锥曲线等用 param_curves：{\"x\": \"3*cos(t)\", \"y\": \"2*sin(t)\", \"from\": 0, \"to\": 6.2832}；circles 画圆。"
    "points 给题干提到的点的坐标；segments、polygons 按点名连线；marks 在坐标轴上标关键刻度，如 "
    "{\"axis\": \"x\", \"value\": 0.5236, \"text\": \"π/6\"}；texts 放必要的文字，如角度、\"古塔\"。\n"
    "4. chart：type 为 histogram 时，bins 按 [左端点, 右端点, 高度] 列出每一组（高度即频率/组距），x_label、y_label 写坐标轴名称；"
    "type 为 bar 时，categories 按 [名称, 数值] 列出。"
    "需要隐藏某一组的数值时加第 4 项（条形图是第 3 项）作为标注：写字母（如 \"a\"）就只标字母，写 \"\" 就不标。\n"
    "图里不能直接给出题目要求的答案：题目要求的长度、角度、某一组的频率或高度等，不要作为数值标在图上，"
    "统计图里待求的那一组按上一条用字母或不标；但题干已经给出的数据照常标出。\n"
    "5. labels 列出要标名字的点（默认全部点），点名照题干写，如 A、B1、P、M。\n"
    "6. 输入里有 teacher_request 时照老师的要求画；figure_required 为 true 表示老师要这张图，不要选 none。"
    "有 last_figure 和 problems_last_time 时，说明上一版图纸和题干对不上，照着这些问题改。\n"
    "只输出 JSON。"
)
# 这份 schema 会原样写进给模型的说明里，所以每一项的写法都写全，模型照着写就不容易出格式错
_NAME_LIST = {"type": "array", "items": {"type": "string"}, "description": "点名，如 [\"A\", \"B\", \"C\"]"}
_NAME_PAIRS = {"type": "array", "items": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2},
               "description": "每条线段写成 [点名, 点名]，如 [[\"P\", \"A\"], [\"A\", \"C\"]]"}
_NUMBER_PAIR = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}
SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["solid", "plane", "chart", "none"]},
        "points": {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "number"}},
                   "description": "{点名: 坐标}，solid 写 [x, y, z]，plane 写 [x, y]"},
        "faces": {"type": "array", "items": _NAME_LIST, "description": "solid 的每个面按顶点顺序列点名"},
        "segments": _NAME_PAIRS,
        "dashed": _NAME_PAIRS,
        "dots": _NAME_LIST,
        "labels": _NAME_LIST,
        "circles": {"type": "array", "items": {"type": "object", "properties": {
            "center": {"description": "点名或坐标"}, "radius": {"type": "number"},
            "plane": {"type": "string", "enum": ["xy", "xz", "yz"]}, "dashed": {"type": "boolean"}},
            "required": ["center", "radius"]}},
        "angle": {"type": "number"},
        "x_range": _NUMBER_PAIR,
        "y_range": _NUMBER_PAIR,
        "axes": {"type": "boolean"},
        "functions": {"type": "array", "items": {"type": "object", "properties": {
            "expr": {"type": "string", "description": "y 关于 x 的表达式，如 \"2*sin(2*x+pi/6)\""},
            "from": {"type": "number"}, "to": {"type": "number"}, "dashed": {"type": "boolean"}},
            "required": ["expr"]}},
        "param_curves": {"type": "array", "items": {"type": "object", "properties": {
            "x": {"type": "string"}, "y": {"type": "string"}, "from": {"type": "number"}, "to": {"type": "number"},
            "dashed": {"type": "boolean"}}, "required": ["x", "y"]}},
        "polygons": {"type": "array", "items": _NAME_LIST},
        "marks": {"type": "array", "items": {"type": "object", "properties": {
            "axis": {"type": "string", "enum": ["x", "y"]}, "value": {"type": "number"}, "text": {"type": "string"}},
            "required": ["axis", "value", "text"]}},
        "texts": {"type": "array", "items": {"type": "object", "properties": {
            "at": {"description": "[x, y] 或点名"}, "text": {"type": "string"}}, "required": ["at", "text"]}},
        "chart": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["histogram", "bar"]},
            "bins": {"type": "array", "items": {"type": "array"},
                     "description": "[左端点, 右端点, 高度] 或 [左端点, 右端点, 高度, 纵轴标注]"},
            "categories": {"type": "array", "items": {"type": "array"},
                           "description": "[名称, 数值] 或 [名称, 数值, 顶上的标注]"},
            "x_label": {"type": "string"}, "y_label": {"type": "string"}}},
    },
    "required": ["kind"],
}


def illustrate(draft: dict, *, call, request: str = "", required: bool = False) -> dict | None:
    """让配图员写图纸、程序画图并核对；第一版有问题就带着问题重画一次。
    request 是老师对这张图的要求；required=True 表示老师点了"配图"，配图员不能回答不需要图。
    返回 {"spec", "svg", "problems", "redrawn"}：problems 是最后一版仍对不上的地方，
    redrawn 是第一版的问题（重画过才有）；两次都没画成时 spec 为 None，problems 是没画成的原因。
    配图员判断不需要配图时返回 None。"""
    stem = draft.get("stem_latex") or draft.get("stem_plain") or ""
    answer = " ".join(str(draft.get(key) or "") for key in ("answer_value", "final_answer"))
    payload = {
        "question_type": draft.get("question_type"),
        "stem": stem,
        "options": draft.get("options") or [],
        "solution": draft.get("full_solution_steps") or draft.get("solution_steps") or [],
        "knowledge": " › ".join(
            str(part) for part in ((draft.get("curriculum") or {}).get(k) for k in ("chapter", "section", "name")) if part
        ),
    }
    if request:
        payload["teacher_request"] = request
    if required:
        payload["figure_required"] = True
    best = None
    first_problems: list[str] = []
    problems: list[str] = []
    for attempt in range(2):
        stage = STAGE_DRAW if attempt == 0 else STAGE_REDRAW
        try:
            raw = call(stage, INSTRUCTIONS, payload, SCHEMA, 3000)
        except Exception as exc:  # noqa: BLE001 - 配图失败不影响题目本身
            logger.warning("配图员调用失败：%s", exc)
            return best or {"spec": None, "svg": "", "problems": [f"配图员没有回应：{exc}"], "redrawn": first_problems}
        try:
            spec = normalize(raw)
            if spec["kind"] == "none":
                if not required:
                    return None
                raise FigureError("老师要给这道题配图，kind 不能是 none，请按题干画一张")
            svg = render(spec)
            problems = check(stem, spec) + reveals_answer(spec, answer, stem)
        except FigureError as exc:
            problems, spec, svg = [str(exc)], None, None
        except Exception as exc:  # noqa: BLE001 - 图纸里有程序没料到的写法，让配图员换一种写法
            logger.warning("图纸画不出来：%s", exc, exc_info=True)
            problems, spec, svg = [f"图纸画不出来（{type(exc).__name__}: {exc}），请检查数据格式"], None, None
        if spec is not None:
            best = {"spec": spec, "svg": svg, "problems": problems, "redrawn": first_problems}
            if not problems:
                return best
        if attempt == 0:
            first_problems = problems
            payload["last_figure"] = raw
            payload["problems_last_time"] = problems
    return best or {"spec": None, "svg": "", "problems": problems, "redrawn": first_problems}


# ---------- Word 里用的图片 ----------


def svg_to_png(svg: str, cache_dir: Path, scale: int = 3) -> bytes | None:
    """用本机的 Edge/Chrome 把 SVG 截成 PNG（Word 不认 SVG）；按内容缓存，同一张图只截一次。"""
    from mingti.export import find_browser  # 放在这里引入，免得和 export 互相引用

    key = hashlib.sha1(f"{scale}:{svg}".encode("utf-8")).hexdigest()[:24]
    target = Path(cache_dir) / f"{key}.png"
    if target.exists():
        return target.read_bytes()
    browser = find_browser()
    size = re.search(r'width="(\d+)" height="(\d+)"', svg)
    if not browser or not size:
        return None
    width, height = size.groups()
    page = f'<!doctype html><meta charset="utf-8"><body style="margin:0;background:#fff;overflow:hidden">{svg}</body>'
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        source = Path(folder) / "figure.html"
        source.write_text(page, encoding="utf-8")
        out = Path(folder) / "figure.png"
        command = [
            browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
            "--no-default-browser-check", f"--user-data-dir={Path(folder) / 'profile'}",
            f"--force-device-scale-factor={scale}", f"--window-size={width},{height}",
            f"--screenshot={out}", source.as_uri(),
        ]
        try:
            subprocess.run(command, capture_output=True, timeout=60, check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return None
        if not out.exists():
            return None
        png = out.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(png)
    return png
