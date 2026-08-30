"""
orthoswift_post.guide
===============================

Generate START_HERE deliverables guides (PDF only) for inspection and agriculture jobs.

The guide content is embedded directly as Python constants — no external file reading at
runtime. This mirrors how report.py bakes in all its content. ReportLab is used for PDF
generation, which is already installed on all OrthoSWIFT worker environments.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# Guide content — embedded at development time from the repo-root .md files

_FIELD_ACTION_GUIDE_SOURCE = """\
# Setup Guide

Your field analysis is complete. Read `prescription_report.pdf` first, then use this guide to load prescriptions onto your machines.

## What's in This Package

| File / Folder | What it is |
|---|---|
| `prescription_report.pdf` | Fertilizer and spray prescription package summary — start here |
| `prescriptions/fertilizer_zones/fertilizer_zones.kml` | Variable-rate management zones — open in Google Earth to review before applying |
| `prescriptions/fertilizer_zones/dji_agras.zip` | DJI Agras fertilizer prescription package |
| `prescriptions/spray_targets/stress_patches.kml` | Spot-spray stress targets — red = severe, yellow = mild |
| `prescriptions/spray_targets/dji_agras.zip` | DJI Agras spot-spray prescription package |
| `technical_gis/rasters/` | GeoTIFF spectral layers (NDVI, NDRE, etc.) for GIS review |
| `technical_gis/data_summaries/` | Zone statistics and canopy cover CSV exports |

## Loading Prescriptions (Single Machine)

1. Open the `.kml` in Google Earth — confirm zones look correct before applying.
2. Open `dji_agras.zip` in the relevant prescription folder.
3. Extract it so the `DJI/` folder is at the USB root.
4. Import the prescription in the DJI Agras workflow and verify the rate raster, units, boundary, and controller preview before application.

The package may contain `DJI/Basemap/orthomosaic.mbtiles`; use it only where the DJI application supports manual offline-layer import.

## TargetRate — Check Which Mode Was Exported

Open `technical_gis/data_summaries/fertilizer_rate_plan.json` and `prescriptions/fertilizer_zones/fertilizer_rate_table.csv` before loading a machine.

| Mode | Meaning | Required action |
|---|---|---|
| `relative` | `TargetRate` is an imagery-derived 0–100% spatial intensity | Assign approved physical product rates in the controller or farm software |
| `physical` | `TargetRate` contains operator/agronomist-supplied rates in the stated unit | Verify product, rate basis, units, rate range, equipment limits, and map preview |

Physical mode is not an OrthoSWIFT fertilizer recommendation. OrthoSWIFT only assigns the supplied plan to the mapped zones. The named approver remains responsible for agronomic suitability. The operator remains responsible for controller interpretation, machine calibration, field boundaries, labels, and legal compliance.

Zone colours show relative vigor, not diagnosis. Whether high- or low-vigor areas receive more product depends on the explicitly approved strategy or zone-rate table.

## Multi-Field Jobs

- **Merged field:** All deliverables are at the ZIP root. If multiple orthos were merged, the seamless mosaic is at `technical_gis/rasters/orthophoto_merged.tif`.
- **Batch (independent fields):** Each field has its own top-level folder (e.g. `Field_1_North_Block/`) containing a complete standalone deliverable set.
"""





# PDF generation

def generate_guide_pdf(out_path: str | Path, *, domain: str) -> Optional[Path]:
    """
    Generate a deliverables guide PDF using ReportLab.

    Content is embedded in this module — no external file reading at runtime.

    Parameters
    ----------
    out_path : str or Path
        Output PDF file path.
    domain : str
        'field_action'

    Returns
    -------
    Path or None
        Output path on success, None if generation fails (non-fatal).
    """
    if domain == "field_action":
        source = _FIELD_ACTION_GUIDE_SOURCE
    else:
        logger.error(f"[guide] Unknown domain: {domain}")
        return None

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import re

        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (
            Paragraph,
            Preformatted,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )

        # Use OrthoSWIFT brand fonts if bundled alongside this module
        plugin_dir = Path(__file__).parent.parent
        USE_SPACE_GROTESK = False
        USE_INTER = False
        for font_file, font_name, flag_attr in [
            ("SpaceGrotesk-Bold.ttf", "SpaceGrotesk", "USE_SPACE_GROTESK"),
            ("InterVariable.ttf",     "Inter",         "USE_INTER"),
        ]:
            p = plugin_dir / font_file
            if p.exists():
                try:
                    pdfmetrics.registerFont(TTFont(font_name, str(p)))
                    if flag_attr == "USE_SPACE_GROTESK":
                        USE_SPACE_GROTESK = True
                    else:
                        USE_INTER = True
                except (OSError, ValueError) as exc:
                    logger.debug("Could not register bundled PDF font %s: %s", p, exc)

        title_font = "SpaceGrotesk" if USE_SPACE_GROTESK else "Helvetica-Bold"
        body_font  = "Inter"        if USE_INTER         else "Helvetica"

        styles = getSampleStyleSheet()

        # Inline markdown helpers
        def _esc(text):
            """Escape XML special chars then apply inline markdown."""
            text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
            text = re.sub(r'\*(.*?)\*',     r'<i>\1</i>', text)
            text = re.sub(
                r'`(.*?)`',
                r'<font face="Courier" size="8.5" color="#1a73e8">\1</font>',
                text,
            )
            return text

        def _para(text, style):
            return Paragraph(_esc(text), style)

        # Named styles
        def _make_style(name, **kw):
            base = kw.pop("parent", styles["Normal"])
            return ParagraphStyle(name, parent=base, **kw)

        ST = {
            "h1": _make_style("H1", fontName=title_font, fontSize=18, leading=22,
                              textColor=colors.HexColor("#1a73e8"),
                              spaceBefore=8, spaceAfter=6),
            "h2": _make_style("H2", fontName=title_font, fontSize=12, leading=15,
                              textColor=colors.HexColor("#1a73e8"),
                              spaceBefore=14, spaceAfter=6, keepWithNext=True),
            "h3": _make_style("H3", fontName=title_font, fontSize=10, leading=13,
                              textColor=colors.HexColor("#495057"),
                              spaceBefore=10, spaceAfter=4, keepWithNext=True),
            "body": _make_style("Body", fontName=body_font, fontSize=9, leading=13,
                                spaceAfter=6),
            "code": _make_style("Code", fontName="Courier", fontSize=8, leading=10,
                                textColor=colors.HexColor("#333333"),
                                backColor=colors.HexColor("#f8f9fa"),
                                borderPadding=6, borderWidth=0.5,
                                borderColor=colors.HexColor("#e9ecef"),
                                spaceAfter=10),
            "bullet": _make_style("Bullet", fontName=body_font, fontSize=9, leading=13,
                                  leftIndent=16, firstLineIndent=-10, spaceAfter=3),
            "numbered": _make_style("Numbered", fontName=body_font, fontSize=9, leading=13,
                                    leftIndent=16, firstLineIndent=-10, spaceAfter=3),
        }

        # Parse lines → flowables
        story = []
        lines = source.splitlines()
        in_code = False
        code_buf = []
        in_table = False
        table_rows = []
        i = 0

        def _flush_table():
            nonlocal in_table, table_rows
            if not table_rows:
                in_table = False
                return
            max_cols = max(len(r) for r in table_rows)
            cell_grid = []
            for ri, row in enumerate(table_rows):
                row += [""] * (max_cols - len(row))
                cell_grid.append([
                    Paragraph(_esc(cell),
                              _make_style(f"TC{ri}{ci}",
                                          fontName=title_font if ri == 0 else body_font,
                                          fontSize=8 if ri == 0 else 7.5, leading=9,
                                          textColor=colors.white if ri == 0
                                                    else colors.HexColor("#333333")))
                    for ci, cell in enumerate(row)
                ])
            col_w = (180 / max_cols) * mm
            tbl = Table(cell_grid, colWidths=[col_w] * max_cols, hAlign="LEFT")
            tbl.setStyle(TableStyle([
                ("BACKGROUND",  (0, 0), (-1,  0), colors.HexColor("#1a73e8")),
                ("GRID",        (0, 0), (-1, -1), 0.5, colors.HexColor("#d0d0d0")),
                ("ALIGN",       (0, 0), (-1, -1), "LEFT"),
                ("VALIGN",      (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING",  (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING",(0, 0), (-1, -1), 6),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.HexColor("#f8f9fa"), colors.white]),
            ]))
            story.append(tbl)
            story.append(Spacer(1, 8))
            in_table = False
            table_rows = []

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            if stripped.startswith("```"):
                if in_code:
                    code_text = "\n".join(code_buf)
                    escaped = (code_text
                               .replace("&", "&amp;")
                               .replace("<", "&lt;")
                               .replace(">", "&gt;"))
                    story.append(Preformatted(escaped, ST["code"]))
                    in_code = False
                    code_buf = []
                else:
                    in_code = True
                i += 1
                continue

            if in_code:
                code_buf.append(line)
                i += 1
                continue

            if stripped.startswith("|"):
                in_table = True
                cells = [c.strip() for c in stripped.split("|")[1:-1]]
                if not all(re.match(r'^:?-+:?$', c) for c in cells):
                    table_rows.append(cells)
                i += 1
                continue
            else:
                if in_table:
                    _flush_table()

            if stripped.startswith("#"):
                level = len(stripped) - len(stripped.lstrip("#"))
                text  = stripped.lstrip("#").strip()
                key = "h1" if level == 1 else "h2" if level == 2 else "h3"
                story.append(_para(text, ST[key]))
                if level == 1:
                    # decorative rule under main title
                    hr = Table([[""]], colWidths=[180 * mm])
                    hr.setStyle(TableStyle([
                        ("LINEBELOW",      (0, 0), (-1, -1), 1.5, colors.HexColor("#1a73e8")),
                        ("TOPPADDING",     (0, 0), (-1, -1), 0),
                        ("BOTTOMPADDING",  (0, 0), (-1, -1), 0),
                    ]))
                    story.append(hr)
                    story.append(Spacer(1, 8))
                i += 1
                continue

            ul = re.match(r'^(\s*)[-*]\s+(.*)$', line)
            if ul:
                indent = len(ul.group(1))
                sty = _make_style(f"BUL{indent}", parent=ST["bullet"],
                                  leftIndent=16 + indent * 10)
                story.append(Paragraph(f"&bull;&nbsp;&nbsp;{_esc(ul.group(2))}", sty))
                i += 1
                continue

            ol = re.match(r'^(\s*)(\d+)\.\s+(.*)$', line)
            if ol:
                indent = len(ol.group(1))
                sty = _make_style(f"OL{indent}", parent=ST["numbered"],
                                  leftIndent=16 + indent * 10)
                story.append(Paragraph(f"{ol.group(2)}.&nbsp;&nbsp;{_esc(ol.group(3))}", sty))
                i += 1
                continue

            if stripped:
                story.append(_para(stripped, ST["body"]))

            i += 1

        # flush trailing table
        if in_table:
            _flush_table()

        # Build document
        def _footer(canvas_obj, doc_obj):
            canvas_obj.saveState()
            canvas_obj.setFont("Helvetica-Oblique", 8)
            canvas_obj.setFillColor(colors.HexColor("#666666"))
            txt = "Deliverables Guide  |  www.orthoswift.net"
            w = doc_obj.pagesize[0]
            canvas_obj.drawString(
                (w - canvas_obj.stringWidth(txt, "Helvetica-Oblique", 8)) / 2,
                10 * mm, txt,
            )
            canvas_obj.restoreState()

        doc = SimpleDocTemplate(
            str(out_path), pagesize=A4,
            leftMargin=15 * mm, rightMargin=15 * mm,
            topMargin=15 * mm, bottomMargin=20 * mm,
        )
        doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
        logger.info(f"[guide] Generated {domain} guide PDF: {out_path}")
        return out_path

    except Exception as exc:
        logger.error(f"[guide] PDF generation failed ({domain}): {exc}",
                     exc_info=True)
        return None


# Public API — called from pipeline.py and analysis_worker.py



def export_guides(out_dir: str | Path) -> tuple[Optional[Path], Optional[Path]]:
    """
    Stub — setup_guide.pdf has been retired.

    The Machine USB Loading Reference is now embedded directly in the official
    Prescription Report (report.py → build_agriculture_pdf). There is
    no longer a separate setup guide deliverable.
    """
    logger.info("[guide] setup_guide.pdf generation skipped — USB reference is embedded in the main report.")
    return None, None
