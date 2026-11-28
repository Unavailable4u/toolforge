"""Render `docs/assets/cover.svg` to `docs/assets/cover.png` (1200 x 630), the social-share image the website (P19) uses.

    python scripts/render_cover.py            # writes docs/assets/cover.png
    python scripts/render_cover.py --out X.png

The SVG is the source; the PNG is generated and not committed. This script has no dependency of its own: it uses the first converter it finds
on the machine, tried in this order, and when there is none it prints the one-liner for each and exits 2 rather than writing a blank file.

    rsvg-convert -w 1200 -h 630 docs/assets/cover.svg -o docs/assets/cover.png
    inkscape docs/assets/cover.svg -w 1200 -h 630 -o docs/assets/cover.png
    magick docs/assets/cover.svg -resize 1200x630 docs/assets/cover.png       (ImageMagick 7; `convert` on version 6)
    python -c "import cairosvg; cairosvg.svg2png(url='docs/assets/cover.svg', write_to='docs/assets/cover.png')"

Proven by `tests/test_docs_p18.py` (the SVG is well formed and says no remote resource; the script's command choice).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "docs" / "assets" / "cover.svg"
PNG = ROOT / "docs" / "assets" / "cover.png"
WIDTH, HEIGHT = 1200, 630


def commands(svg: Path, out: Path) -> list[tuple[str, list[str]]]:
    """Every converter command this script knows, in order of preference (whether installed or not)."""
    return [
        ("rsvg-convert", ["rsvg-convert", "-w", str(WIDTH), "-h", str(HEIGHT), str(svg), "-o", str(out)]),
        ("inkscape", ["inkscape", str(svg), "-w", str(WIDTH), "-h", str(HEIGHT), "-o", str(out)]),
        ("magick", ["magick", "-background", "none", str(svg), "-resize", f"{WIDTH}x{HEIGHT}", str(out)]),
        ("convert", ["convert", "-background", "none", str(svg), "-resize", f"{WIDTH}x{HEIGHT}", str(out)]),
    ]


def render(svg: Path, out: Path) -> str | None:
    """The name of the converter that made `out`, or None when none worked."""
    for name, argv in commands(svg, out):
        if shutil.which(name) is None:
            continue
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0 and out.is_file() and out.stat().st_size > 0:
            return name
        out.unlink(missing_ok=True)
    try:
        import cairosvg  # type: ignore[import-not-found]

        cairosvg.svg2png(url=str(svg), write_to=str(out), output_width=WIDTH, output_height=HEIGHT)
        return "cairosvg" if out.is_file() and out.stat().st_size > 0 else None
    except Exception:  # not installed, or a library it needs is missing
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the cover image to PNG.")
    parser.add_argument("--out", default=str(PNG))
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    used = render(SVG, out)
    if used is None:
        print(
            "no SVG converter worked here. Install one of these and run again, or run the command yourself:",
            file=sys.stderr,
        )
        for _, command in commands(SVG.relative_to(ROOT), out):
            print("  " + " ".join(command), file=sys.stderr)
        return 2
    print(f"wrote {out} with {used}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
