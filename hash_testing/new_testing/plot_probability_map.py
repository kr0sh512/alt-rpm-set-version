#!/usr/bin/env python3
"""Render avalanche bit-probability CSV files as two PNG bar charts."""

from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont

BACKGROUND = "#f7f8fa"
PANEL = "#ffffff"
GRID = "#d9dee7"
TEXT = "#172033"
MUTED = "#637083"
REFERENCE = "#d24b4b"
COLORS = (
    "#377eb8",
    "#4daf4a",
    "#984ea3",
    "#ff7f00",
    "#e41a1c",
    "#00a6a6",
    "#a65628",
)


@dataclass(frozen=True)
class ProbabilityRow:
    operation: str
    pairs: int
    probabilities: list[float]


@dataclass(frozen=True)
class ChartScale:
    minimum: float
    maximum: float
    ticks: tuple[float, ...]


def nice_step(value: float) -> float:
    exponent = math.floor(math.log10(value))
    fraction = value / 10**exponent
    nice_fraction = min((1.0, 2.0, 2.5, 5.0, 10.0), key=lambda item: abs(item - fraction))
    return nice_fraction * 10**exponent


def make_scale(
    values: Sequence[float],
    *,
    hard_limits: tuple[float, float],
    reference: float | None = None,
) -> ChartScale:
    """Build a padded shared scale constrained to hard limits."""
    hard_minimum, hard_maximum = hard_limits
    finite_values = [value for value in values if math.isfinite(value)]
    if reference is not None:
        finite_values.append(reference)
    if not finite_values:
        finite_values = [hard_minimum, hard_maximum]

    minimum = min(finite_values)
    maximum = max(finite_values)
    if minimum == maximum:
        expansion = (hard_maximum - hard_minimum) * 0.1
        minimum -= expansion / 2
        maximum += expansion / 2

    span = maximum - minimum
    padded_minimum = max(hard_minimum, minimum - span * 0.1)
    padded_maximum = min(hard_maximum, maximum + span * 0.1)
    step = nice_step(max((padded_maximum - padded_minimum) / 5, 1e-12))
    scaled_minimum = max(hard_minimum, math.floor(padded_minimum / step) * step)
    scaled_maximum = min(hard_maximum, math.ceil(padded_maximum / step) * step)
    if scaled_minimum == scaled_maximum:
        scaled_minimum, scaled_maximum = hard_minimum, hard_maximum

    tick_count = round((scaled_maximum - scaled_minimum) / step)
    ticks = [scaled_minimum + index * step for index in range(tick_count + 1)]
    if reference is not None and scaled_minimum <= reference <= scaled_maximum:
        ticks.append(reference)
    normalized_ticks = tuple(
        sorted({round(value, 12) for value in ticks if scaled_minimum <= value <= scaled_maximum})
    )
    return ChartScale(scaled_minimum, scaled_maximum, normalized_ticks)


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = (
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def read_probability_map(path: Path) -> list[ProbabilityRow]:
    """Read operation rows and numerically ordered bit columns from a CSV file."""
    try:
        stream = path.open(encoding="utf-8", newline="")
    except OSError as error:
        raise ValueError(f"не удалось открыть {path}: {error}") from error

    with stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        if not fields or "operation" not in fields or "pairs" not in fields:
            raise ValueError("CSV должен содержать колонки operation и pairs")

        bit_fields: list[tuple[int, str]] = []
        for field in fields:
            if not field.startswith("bit_"):
                continue
            try:
                bit_fields.append((int(field.removeprefix("bit_")), field))
            except ValueError as error:
                raise ValueError(f"некорректная битовая колонка: {field}") from error
        bit_fields.sort()
        if not bit_fields:
            raise ValueError("CSV не содержит колонок bit_N")
        expected_bits = list(range(len(bit_fields)))
        actual_bits = [bit for bit, _field in bit_fields]
        if actual_bits != expected_bits:
            raise ValueError("битовые колонки должны непрерывно идти от bit_0")

        rows: list[ProbabilityRow] = []
        for line_number, row in enumerate(reader, start=2):
            operation = (row.get("operation") or "").strip()
            if not operation:
                raise ValueError(f"строка {line_number}: пустая операция")
            try:
                pairs = int(row["pairs"] or "")
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"строка {line_number}: некорректное число пар"
                ) from error
            if pairs < 0:
                raise ValueError(f"строка {line_number}: число пар меньше нуля")

            probabilities: list[float] = []
            for _bit, field in bit_fields:
                try:
                    value = float(row[field] or "")
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        f"строка {line_number}: некорректное значение {field}"
                    ) from error
                if not math.isnan(value) and not 0.0 <= value <= 1.0:
                    raise ValueError(
                        f"строка {line_number}: {field} должен быть от 0 до 1"
                    )
                probabilities.append(value)
            rows.append(ProbabilityRow(operation, pairs, probabilities))

    if not rows:
        raise ValueError("CSV не содержит строк с операциями")
    return rows


def mean_absolute_deviation(probabilities: Sequence[float]) -> float:
    """Return the mean |p - 0.5| over finite bit probabilities."""
    deviations = [
        abs(probability - 0.5)
        for probability in probabilities
        if math.isfinite(probability)
    ]
    return fmean(deviations) if deviations else float("nan")


def text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return round(box[2] - box[0])


def draw_centered_text(
    draw: ImageDraw.ImageDraw,
    center_x: float,
    y: float,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: str = TEXT,
) -> None:
    draw.text(
        (center_x - text_width(draw, text, font) / 2, y),
        text,
        font=font,
        fill=fill,
    )


def draw_bit_panel(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int, int, int],
    row: ProbabilityRow,
    color: str,
    scale: ChartScale,
) -> None:
    left, top, right, bottom = bounds
    title_font = load_font(22, bold=True)
    label_font = load_font(14)
    tick_font = load_font(12)

    draw.rounded_rectangle(bounds, radius=12, fill=PANEL, outline=GRID, width=1)
    draw.text((left + 18, top + 13), row.operation, font=title_font, fill=TEXT)
    pairs_text = f"pairs: {row.pairs}"
    draw.text(
        (right - 18 - text_width(draw, pairs_text, label_font), top + 17),
        pairs_text,
        font=label_font,
        fill=MUTED,
    )

    plot_left = left + 54
    plot_right = right - 18
    plot_top = top + 55
    plot_bottom = bottom - 42
    plot_height = plot_bottom - plot_top

    scale_span = scale.maximum - scale.minimum
    for probability in scale.ticks:
        y = round(plot_bottom - (probability - scale.minimum) / scale_span * plot_height)
        line_color = REFERENCE if probability == 0.5 else GRID
        line_width = 2 if probability == 0.5 else 1
        draw.line((plot_left, y, plot_right, y), fill=line_color, width=line_width)
        label = f"{probability:.3g}"
        draw.text(
            (plot_left - 8 - text_width(draw, label, tick_font), y - 7),
            label,
            font=tick_font,
            fill=MUTED,
        )

    bit_count = len(row.probabilities)
    slot_width = (plot_right - plot_left) / bit_count
    bar_width = max(1, int(slot_width * 0.72))
    for bit, probability in enumerate(row.probabilities):
        if not math.isfinite(probability):
            continue
        center = plot_left + (bit + 0.5) * slot_width
        x0 = round(center - bar_width / 2)
        x1 = round(center + bar_width / 2)
        y = round(
            plot_bottom
            - (probability - scale.minimum) / scale_span * plot_height
        )
        draw.rectangle((x0, y, x1, plot_bottom), fill=color)

    tick_step = max(1, math.ceil(bit_count / 16))
    for bit in range(0, bit_count, tick_step):
        center = plot_left + (bit + 0.5) * slot_width
        label = str(bit)
        draw.text(
            (center - text_width(draw, label, tick_font) / 2, plot_bottom + 7),
            label,
            font=tick_font,
            fill=MUTED,
        )
    draw_centered_text(
        draw,
        (plot_left + plot_right) / 2,
        bottom - 21,
        "output bit (0 = LSB)",
        tick_font,
        MUTED,
    )


def render_bit_probabilities(
    rows: Sequence[ProbabilityRow], output: Path, title: str
) -> None:
    columns = 2 if len(rows) > 1 else 1
    panel_width = 760
    panel_height = 330
    gap = 18
    margin = 24
    title_height = 70
    row_count = math.ceil(len(rows) / columns)
    width = margin * 2 + columns * panel_width + (columns - 1) * gap
    height = title_height + margin + row_count * panel_height + (row_count - 1) * gap

    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    scale = make_scale(
        [probability for row in rows for probability in row.probabilities],
        reference=0.5,
        hard_limits=(0.0, 1.0),
    )
    draw_centered_text(draw, width / 2, 18, title, load_font(30, bold=True))
    draw_centered_text(
        draw,
        width / 2,
        52,
        f"Shared scale {scale.minimum:.3g}–{scale.maximum:.3g}; red line = ideal p=0.5",
        load_font(14),
        MUTED,
    )

    for index, row in enumerate(rows):
        column = index % columns
        grid_row = index // columns
        left = margin + column * (panel_width + gap)
        top = title_height + grid_row * (panel_height + gap)
        draw_bit_panel(
            draw,
            (left, top, left + panel_width, top + panel_height),
            row,
            COLORS[index % len(COLORS)],
            scale,
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, "PNG", optimize=True)


def render_mean_deviations(
    rows: Sequence[ProbabilityRow], output: Path, title: str
) -> None:
    width, height = 1200, 720
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw_centered_text(draw, width / 2, 22, title, load_font(30, bold=True))
    draw_centered_text(
        draw,
        width / 2,
        58,
        "Mean absolute deviation from ideal avalanche probability: mean(|p - 0.5|)",
        load_font(15),
        MUTED,
    )

    plot_left, plot_right = 90, width - 40
    plot_top, plot_bottom = 110, height - 120
    plot_height = plot_bottom - plot_top
    deviations = [mean_absolute_deviation(row.probabilities) for row in rows]
    scale = make_scale(deviations, hard_limits=(0.0, 0.5))
    scale_span = scale.maximum - scale.minimum
    tick_font = load_font(13)
    label_font = load_font(15)
    value_font = load_font(14, bold=True)

    for value in scale.ticks:
        y = round(plot_bottom - (value - scale.minimum) / scale_span * plot_height)
        draw.line((plot_left, y, plot_right, y), fill=GRID, width=1)
        label = f"{value:.3g}"
        draw.text(
            (plot_left - 10 - text_width(draw, label, tick_font), y - 7),
            label,
            font=tick_font,
            fill=MUTED,
        )

    slot_width = (plot_right - plot_left) / len(rows)
    bar_width = min(105, max(20, int(slot_width * 0.62)))
    for index, (row, deviation) in enumerate(zip(rows, deviations, strict=True)):
        center = plot_left + (index + 0.5) * slot_width
        x0 = round(center - bar_width / 2)
        x1 = round(center + bar_width / 2)
        if math.isfinite(deviation):
            y = round(
                plot_bottom
                - (deviation - scale.minimum) / scale_span * plot_height
            )
            draw.rectangle((x0, y, x1, plot_bottom), fill=COLORS[index % len(COLORS)])
            value_label = f"{deviation:.4f}"
        else:
            y = plot_bottom
            value_label = "n/a"
        draw_centered_text(draw, center, max(plot_top, y - 22), value_label, value_font)
        draw_centered_text(draw, center, plot_bottom + 12, row.operation, label_font)
        draw_centered_text(
            draw, center, plot_bottom + 36, f"pairs: {row.pairs}", tick_font, MUTED
        )

    draw.line((plot_left, plot_top, plot_left, plot_bottom), fill=TEXT, width=2)
    draw.line((plot_left, plot_bottom, plot_right, plot_bottom), fill=TEXT, width=2)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, "PNG", optimize=True)


def generate_plots(source: Path, output_directory: Path | None = None) -> tuple[Path, Path]:
    rows = read_probability_map(source)
    destination = output_directory or source.parent
    bit_output = destination / f"{source.stem}_bits.png"
    deviation_output = destination / f"{source.stem}_deviation.png"
    display_name = source.stem

    render_bit_probabilities(rows, bit_output, f"{display_name}: per-bit avalanche map")
    render_mean_deviations(
        rows,
        deviation_output,
        f"{display_name}: deviation from p=0.5",
    )
    return bit_output, deviation_output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Создаёт две PNG-гистограммы из CSV вероятностной карты: "
            "вероятности по битам для каждой операции и среднее |p-0.5|."
        )
    )
    parser.add_argument("csv_file", type=Path, help="CSV из probability_map.py")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="каталог PNG (по умолчанию каталог исходного CSV)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        outputs = generate_plots(args.csv_file, args.output_dir)
    except ValueError as error:
        raise SystemExit(f"error: {error}") from error

    for output in outputs:
        print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
