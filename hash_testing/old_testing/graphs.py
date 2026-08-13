#!/usr/bin/env python3
"""Convert hash benchmark CSV outputs into SVG graphs.

Input directories:
  speed/*.out
  collizions/*.out
  collizions_ascii/*.out

Output:
  graphs/speed.svg
  graphs/collizions.svg
  graphs/collizions_ascii.svg
"""

from __future__ import annotations

import csv
import html
import math
from collections import defaultdict
from pathlib import Path
from typing import Callable, Iterable

ROOT = Path(__file__).resolve().parent
GRAPH_DIR = ROOT / "graphs"
HASH_ORDER = ["xxh32", "xxh64", "xxh3_64", "city32", "city64", "joaat"]
COLORS = {
    "xxh32": "#1f77b4",
    "xxh64": "#ff7f0e",
    "xxh3_64": "#2ca02c",
    "city32": "#d62728",
    "city64": "#9467bd",
    "joaat": "#8c564b",
}


def read_rows(test_name: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted((ROOT / test_name).glob("*.out")):
        with path.open(newline="") as f:
            rows.extend(csv.DictReader(f))
    if not rows:
        raise FileNotFoundError(f"no .out files found in {ROOT / test_name}")
    return rows


def log_scale(values: Iterable[float], lo_px: float, hi_px: float) -> Callable[[float], float]:
    vals = [v for v in values if v > 0]
    lo = min(vals)
    hi = max(vals)
    if lo == hi:
        return lambda _v: (lo_px + hi_px) / 2.0
    log_lo = math.log10(lo)
    log_hi = math.log10(hi)
    return lambda v: lo_px + (math.log10(max(v, lo)) - log_lo) / (log_hi - log_lo) * (hi_px - lo_px)


def linear_scale(values: Iterable[float], lo_px: float, hi_px: float) -> Callable[[float], float]:
    vals = list(values)
    lo = min(vals)
    hi = max(vals)
    if lo == hi:
        return lambda _v: (lo_px + hi_px) / 2.0
    return lambda v: lo_px + (v - lo) / (hi - lo) * (hi_px - lo_px)


def nice_ticks(max_value: float, count: int = 5) -> list[float]:
    if max_value <= 0:
        return [0.0]
    raw = max_value / max(count - 1, 1)
    power = 10 ** math.floor(math.log10(raw))
    step = min((1, 2, 5, 10), key=lambda x: abs(x * power - raw)) * power
    ticks = [0.0]
    value = step
    while value <= max_value * 1.001:
        ticks.append(value)
        value += step
    return ticks


def fmt_num(value: float) -> str:
    if value == 0:
        return "0"
    if abs(value) < 0.01 or abs(value) >= 10000:
        return f"{value:.2e}"
    if abs(value) < 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{value:.2f}".rstrip("0").rstrip(".")


def polyline(points: list[tuple[float, float]], color: str) -> str:
    pts = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    circles = "".join(
        f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="{color}" />'
        for x, y in points
    )
    return (
        f'<polyline points="{pts}" fill="none" stroke="{color}" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />{circles}'
    )


def legend(x: int, y: int) -> str:
    parts = []
    for i, name in enumerate(HASH_ORDER):
        yy = y + i * 18
        color = COLORS[name]
        parts.append(f'<rect x="{x}" y="{yy - 10}" width="12" height="12" fill="{color}" />')
        parts.append(f'<text x="{x + 18}" y="{yy}" class="legend">{html.escape(name)}</text>')
    return "\n".join(parts)


def chart(
    *,
    title: str,
    series: dict[str, list[tuple[float, float]]],
    x_label: str,
    y_label: str,
    width: int,
    height: int,
    x_log: bool = True,
) -> str:
    margin_l, margin_r, margin_t, margin_b = 72, 150, 42, 58
    plot_x0, plot_x1 = margin_l, width - margin_r
    plot_y0, plot_y1 = margin_t, height - margin_b

    all_points = [pt for points in series.values() for pt in points]
    xs = [x for x, _ in all_points]
    ys = [y for _, y in all_points]
    x_map = log_scale(xs, plot_x0, plot_x1) if x_log else linear_scale(xs, plot_x0, plot_x1)
    y_max = max(ys) if ys else 1.0
    y_map_linear = linear_scale([0.0, y_max], plot_y1, plot_y0)

    parts = [
        f'<g class="chart">',
        f'<text x="{width / 2:.0f}" y="22" text-anchor="middle" class="title">{html.escape(title)}</text>',
        f'<rect x="{plot_x0}" y="{plot_y0}" width="{plot_x1 - plot_x0}" height="{plot_y1 - plot_y0}" class="plot-bg" />',
    ]

    x_ticks = sorted(set(xs))
    for x in x_ticks:
        px = x_map(x)
        parts.append(f'<line x1="{px:.2f}" y1="{plot_y0}" x2="{px:.2f}" y2="{plot_y1}" class="grid" />')
        parts.append(f'<text x="{px:.2f}" y="{plot_y1 + 18}" text-anchor="middle" class="tick">{fmt_num(x)}</text>')

    for y in nice_ticks(y_max):
        py = y_map_linear(y)
        parts.append(f'<line x1="{plot_x0}" y1="{py:.2f}" x2="{plot_x1}" y2="{py:.2f}" class="grid" />')
        parts.append(f'<text x="{plot_x0 - 8}" y="{py + 4:.2f}" text-anchor="end" class="tick">{fmt_num(y)}</text>')

    parts.append(f'<line x1="{plot_x0}" y1="{plot_y1}" x2="{plot_x1}" y2="{plot_y1}" class="axis" />')
    parts.append(f'<line x1="{plot_x0}" y1="{plot_y0}" x2="{plot_x0}" y2="{plot_y1}" class="axis" />')
    parts.append(f'<text x="{(plot_x0 + plot_x1) / 2:.0f}" y="{height - 14}" text-anchor="middle" class="label">{html.escape(x_label)}</text>')
    parts.append(f'<text transform="translate(18,{(plot_y0 + plot_y1) / 2:.0f}) rotate(-90)" text-anchor="middle" class="label">{html.escape(y_label)}</text>')

    for name in HASH_ORDER:
        points = series.get(name, [])
        if not points:
            continue
        mapped = [(x_map(x), y_map_linear(y)) for x, y in sorted(points)]
        parts.append(polyline(mapped, COLORS[name]))

    parts.append(legend(width - margin_r + 26, margin_t + 16))
    parts.append("</g>")
    return "\n".join(parts)


def svg_page(width: int, height: int, body: str) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<style>
  .title {{ font: 700 18px sans-serif; fill: #111; }}
  .label {{ font: 13px sans-serif; fill: #222; }}
  .tick {{ font: 11px sans-serif; fill: #555; }}
  .legend {{ font: 12px monospace; fill: #222; }}
  .axis {{ stroke: #333; stroke-width: 1.2; }}
  .grid {{ stroke: #ddd; stroke-width: 0.8; }}
  .plot-bg {{ fill: #fff; stroke: #bbb; stroke-width: 1; }}
</style>
<rect width="100%" height="100%" fill="#fff" />
{body}
</svg>
'''


def make_speed() -> None:
    rows = read_rows("speed")
    series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        series[row["hash"]].append((float(row["length"]), float(row["median_gib_per_s"])))

    body = chart(
        title="Hash speed on random bytes (median GiB/s)",
        series=series,
        x_label="input length, bytes (log scale)",
        y_label="median GiB/s",
        width=1100,
        height=620,
    )
    (GRAPH_DIR / "speed.svg").write_text(svg_page(1100, 620, body))


def collision_series(rows: list[dict[str, str]], bits: int) -> dict[str, list[tuple[float, float]]]:
    series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        length = float(row["length"])
        if length == 1:
            continue
        if int(row["bits"]) == bits:
            series[row["hash"]].append((length, float(row["median_collision_rate"])))
    return series


def make_collision(test_name: str, title_prefix: str) -> None:
    rows = read_rows(test_name)
    bits_values = sorted({int(row["bits"]) for row in rows})
    chart_w, chart_h = 1100, 420
    body_parts = []
    for i, bits in enumerate(bits_values):
        sub = chart(
            title=f"{title_prefix}: {bits}-bit truncated hash collision rate",
            series=collision_series(rows, bits),
            x_label="input length, bytes/chars (log scale)",
            y_label="median collision rate",
            width=chart_w,
            height=chart_h,
        )
        body_parts.append(f'<g transform="translate(0,{i * chart_h})">{sub}</g>')

    height = chart_h * len(bits_values)
    (GRAPH_DIR / f"{test_name}.svg").write_text(svg_page(chart_w, height, "\n".join(body_parts)))


def main() -> int:
    GRAPH_DIR.mkdir(exist_ok=True)
    make_speed()
    make_collision("collizions", "Random bytes")
    make_collision("collizions_ascii", "Random a-zA-Z strings")
    print(f"wrote {GRAPH_DIR / 'speed.svg'}")
    print(f"wrote {GRAPH_DIR / 'collizions.svg'}")
    print(f"wrote {GRAPH_DIR / 'collizions_ascii.svg'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
