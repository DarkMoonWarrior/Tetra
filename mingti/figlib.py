"""配图的绘图库（改自 D:\\高中数学\\模拟卷\\figlib.py，模拟卷的配图就是用它画的）：
Solid  —— 斜二测投影的立体图，线段被面遮挡的部分自动画成虚线；
Plot2D —— 平面直角坐标系中的函数图像、圆锥曲线、点和线段。
两者的 .svg() 都返回 <svg> 字符串。和原版不同的是线型、字体直接写在元素属性上，
不依赖外部样式表，所以网页、打印页、Word 里的图片看起来都一样。"""

import html
import itertools
import math
import re

import numpy as np

STYLE = {
    "ln": 'fill="none" stroke="#000" stroke-width="1.3" stroke-linejoin="round" stroke-linecap="round"',
    "thin": 'fill="none" stroke="#000" stroke-width="0.9"',
    "dash": 'fill="none" stroke="#000" stroke-width="1.1" stroke-dasharray="5 3"',
    "dot": 'fill="#000"',
}
FONT = 'font-family="Times New Roman, serif" font-size="15"'
_marker_ids = itertools.count(1)


def label_svg(x, y, name, upright=False):
    """'A1' → A 加下标 1；"A'" 原样；upright=True 时用正体（数值、文字说明）。"""
    style = 'font-style="normal"' if upright else 'font-style="italic"'
    m = re.fullmatch(r"([A-Za-z]'*)(\d+)", name)
    if m and not upright:
        return (
            f'<text x="{x:.1f}" y="{y:.1f}" {FONT} {style}>{html.escape(m.group(1))}'
            f'<tspan font-style="normal" font-size="10" dy="4">{m.group(2)}</tspan></text>'
        )
    return f'<text x="{x:.1f}" y="{y:.1f}" {FONT} {style}>{html.escape(name)}</text>'


def _inside(pt, poly, tol):
    """pt 在多边形 poly 内部且离边界超过 tol。"""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
        ex, ey = x2 - x1, y2 - y1
        L2 = ex * ex + ey * ey
        t = 0 if L2 == 0 else max(0, min(1, ((x - x1) * ex + (y - y1) * ey) / L2))
        if math.hypot(x - x1 - t * ex, y - y1 - t * ey) < tol:
            return False
    return inside


class Solid:
    def __init__(self, pts, faces=(), k=0.5, ang=45, scale=40):
        """pts: {名称: (x, y, z)}，y 为纵深方向；faces: 遮挡面（顶点名元组）。
        ang 为纵深轴在纸面上的倾角（度），45 表示向右上方，135 表示向左上方。"""
        self.P = {n: np.array(p, float) for n, p in pts.items()}
        self.faces = [tuple(f) for f in faces]
        self.k, self.a, self.s = k, math.radians(ang), scale
        self.d = np.array([-k * math.cos(self.a), 1.0, -k * math.sin(self.a)])  # 投影方向（指向纸内）
        self.items, self.texts = [], []

    def pr(self, p):
        p = np.asarray(p, float)
        return np.array([p[0] + self.k * p[1] * math.cos(self.a), p[2] + self.k * p[1] * math.sin(self.a)]) * self.s

    def pt(self, p):
        return self.P[p] if isinstance(p, str) else np.asarray(p, float)

    def hidden(self, q):
        q2 = self.pr(q)
        for f in self.faces:
            V = [self.P[n] for n in f]
            nrm = np.cross(V[1] - V[0], V[2] - V[0])
            den = nrm @ self.d
            if abs(den) < 1e-12:
                continue
            t = nrm @ (V[0] - q) / den
            if t < -1e-7 and _inside(q2, [self.pr(v) for v in V], 1e-6 * self.s):
                return True
        return False

    def seg(self, a, b, style="auto", n=80):
        """画线段 a→b（点名或坐标）。style: auto 自动判断虚实，ln 实线，dash 虚线，thin 细线。"""
        A, B = self.pt(a), self.pt(b)
        if style != "auto":
            self.items.append((style, A, B))
            return
        ts = np.linspace(0, 1, n + 1)
        hid = [self.hidden(A + (B - A) * t) for t in ts]
        start = 0
        for i in range(1, n + 2):
            if i == n + 1 or hid[i] != hid[start]:
                t0, t1 = ts[start], ts[min(i, n)]
                self.items.append(("dash" if hid[start] else "ln", A + (B - A) * t0, A + (B - A) * t1))
                start = i

    def dot(self, p):
        self.items.append(("dot", self.pt(p), None))

    def label(self, name, at=None, dx=None, dy=None):
        """在点 at（默认同名点）旁标注；不给偏移时沿"重心→该点"方向向外放。"""
        p = self.pt(at if at is not None else name)
        self.texts.append((name, p, dx, dy))

    def svg(self, margin=16):
        pts2 = [self.pr(p) for p in self.P.values()]
        cen = np.mean(pts2, axis=0)
        segs, dots = [], []
        for st, A, B in self.items:
            if st == "dot":
                dots.append(self.pr(A))
            else:
                segs.append((st, self.pr(A), self.pr(B)))
        labs = []
        for name, p, dx, dy in self.texts:
            q = self.pr(p)
            if dx is None:
                v = q - cen
                v = v / (np.linalg.norm(v) + 1e-9)
                dx, dy = 13 * v[0] - 5, -13 * v[1] + 5
            labs.append((name, q[0] + dx, -q[1] + dy))
        allp = pts2 + [a for _, a, b in segs] + [b for _, a, b in segs]
        xs = [q[0] for q in allp] + [l[1] for l in labs] + [l[1] + 14 for l in labs]
        ys = [-q[1] for q in allp] + [l[2] for l in labs] + [l[2] - 13 for l in labs]
        x0, y0 = min(xs) - margin, min(ys) - margin
        W, H = max(xs) - x0 + margin, max(ys) - y0 + margin
        out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" viewBox="0 0 {W:.0f} {H:.0f}">']
        for st, a, b in segs:
            out.append(
                f'<line {STYLE[st]} x1="{a[0] - x0:.1f}" y1="{-a[1] - y0:.1f}" x2="{b[0] - x0:.1f}" y2="{-b[1] - y0:.1f}"/>'
            )
        for a in dots:
            out.append(f'<circle {STYLE["dot"]} cx="{a[0] - x0:.1f}" cy="{-a[1] - y0:.1f}" r="2"/>')
        for name, x, y in labs:
            out.append(label_svg(x - x0, y - y0, name))
        out.append("</svg>")
        return "".join(out)


class Plot2D:
    """平面直角坐标系。xr、yr 为显示范围，unit 为每单位像素数（可分别给 ux、uy）。
    axes=False 时不画坐标轴，用来画不带坐标系的平面图形。"""

    def __init__(self, xr, yr, unit=30, uy=None, axes=True, arrow=True):
        self.xr, self.yr, self.ux, self.uy = xr, yr, unit, uy or unit
        self.items = []
        self.marker = f"ar{next(_marker_ids)}"
        if axes:
            mk = f' marker-end="url(#{self.marker})"' if arrow else ""
            if xr[0] <= 0 <= xr[1] and yr[0] <= 0 <= yr[1]:
                self.items.append(f'<line {STYLE["thin"]} x1="{self.X(xr[0])}" y1="{self.Y(0)}" x2="{self.X(xr[1])}" y2="{self.Y(0)}"{mk}/>')
                self.items.append(f'<line {STYLE["thin"]} x1="{self.X(0)}" y1="{self.Y(yr[0])}" x2="{self.X(0)}" y2="{self.Y(yr[1])}"{mk}/>')
                self.text(xr[1], 0, "x", 4, 5)  # 写在箭头右边，免得和 x 轴下面的刻度文字挤在一起
                self.text(0, yr[1], "y", 6, 8)
                self.text(0, 0, "O", -14, 15)

    def X(self, x):
        return round((x - self.xr[0]) * self.ux + 14, 1)

    def Y(self, y):
        return round((self.yr[1] - y) * self.uy + 14, 1)

    def curve(self, f, a, b, n=400, cls="ln", param=False):
        """param=False：y=f(x)，x∈[a,b]；param=True：f(t)→(x,y)，t∈[a,b]。超出显示范围的部分断开。"""
        pts, path = [], []
        for t in np.linspace(a, b, n + 1):
            try:
                x, y = f(t) if param else (t, f(t))
            except (ValueError, ZeroDivisionError, OverflowError, TypeError):
                x, y = None, None
            ok = (
                y is not None
                and x is not None
                and np.isfinite(y)
                and np.isfinite(x)
                and self.yr[0] - 0.05 <= y <= self.yr[1] + 0.05
                and self.xr[0] - 0.05 <= x <= self.xr[1] + 0.05
            )
            if ok:
                pts.append(f"{self.X(x)},{self.Y(y)}")
            elif pts:
                path.append(pts)
                pts = []
        if pts:
            path.append(pts)
        for p in path:
            if len(p) > 1:
                self.items.append(f'<polyline {STYLE[cls]} points="{" ".join(p)}"/>')

    def line(self, p, q, cls="ln"):
        self.items.append(f'<line {STYLE[cls]} x1="{self.X(p[0])}" y1="{self.Y(p[1])}" x2="{self.X(q[0])}" y2="{self.Y(q[1])}"/>')

    def arrow_line(self, p, q):
        """带箭头的细线（统计图的坐标轴）。"""
        self.items.append(
            f'<line {STYLE["thin"]} x1="{self.X(p[0])}" y1="{self.Y(p[1])}" x2="{self.X(q[0])}" y2="{self.Y(q[1])}" '
            f'marker-end="url(#{self.marker})"/>'
        )

    def poly(self, pts, cls="ln", close=False):
        s = " ".join(f"{self.X(x)},{self.Y(y)}" for x, y in pts)
        self.items.append(f'<{"polygon" if close else "polyline"} {STYLE[cls]} points="{s}"/>')

    def dot(self, p, r=2):
        self.items.append(f'<circle {STYLE["dot"]} cx="{self.X(p[0])}" cy="{self.Y(p[1])}" r="{r}"/>')

    def text(self, x, y, s, dx=0, dy=0, upright=False):
        self.items.append(label_svg(self.X(x) + dx, self.Y(y) + dy, s, upright=upright))

    def svg(self):
        W = self.X(self.xr[1]) + 16
        H = self.Y(self.yr[0]) + 16
        defs = (
            f'<defs><marker id="{self.marker}" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">'
            '<path d="M0,1 L7,4 L0,7" fill="none" stroke="#000" stroke-width="1"/></marker></defs>'
        )
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" viewBox="0 0 {W:.0f} {H:.0f}">'
            + defs
            + "".join(self.items)
            + "</svg>"
        )
