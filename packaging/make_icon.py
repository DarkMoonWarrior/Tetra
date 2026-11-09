"""生成桌面程序的图标 packaging/tetra.ico：和网页的小图标同一个样子（黑底圆角方块里一个白色线框四面体，
看不见的那条棱画成虚线）。每个尺寸单独从 SVG 画，小尺寸也清楚；ICO 里直接放 PNG（Windows Vista 以后都认）。

    .venv\\Scripts\\python packaging\\make_icon.py
"""

from __future__ import annotations

import struct
from pathlib import Path

import pymupdf

HERE = Path(__file__).resolve().parent
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

def _dashes(x0: float, y0: float, x1: float, y1: float, dash: float = 2.0, gap: float = 2.3) -> str:
    """虚线一段段写出来（MuPDF 画 SVG 时不认 stroke-dasharray）。"""
    length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    parts, t = [], 0.0
    while t < length:
        a, b = t / length, min(t + dash, length) / length
        parts.append(f"M{x0 + (x1 - x0) * a:.2f} {y0 + (y1 - y0) * a:.2f} {x0 + (x1 - x0) * b:.2f} {y0 + (y1 - y0) * b:.2f}")
        t += dash + gap
    return " ".join(parts)


SVG = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32' width='{size}' height='{size}'>
<rect width='32' height='32' rx='8' fill='#0d0d0d'/>
<g fill='none' stroke='#ffffff' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'>
<path d='M15.8 5.5 6 21.5l7.6 5.2 12.6-6.1Z'/>
<path d='M15.8 5.5 13.6 26.7'/>
<path d='""" + _dashes(6, 21.5, 26.2, 20.6) + """' stroke-width='1.4'/>
</g></svg>"""


def png(size: int) -> bytes:
    with pymupdf.open(stream=SVG.format(size=size).encode(), filetype="svg") as document:
        page = document[0]
        scale = size / page.rect.width
        return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=True).tobytes("png")


def main() -> Path:
    images = [(size, png(size)) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        side = 0 if size >= 256 else size  # 256 写成 0
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    target = HERE / "tetra.ico"
    target.write_bytes(header + entries + blobs)
    (HERE / "tetra.png").write_bytes(images[-1][1])  # 256 像素的 PNG：README、发布页上用
    return target


if __name__ == "__main__":
    print(main())
