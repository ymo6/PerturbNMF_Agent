"""figure_extract.py on synthetic PDFs built with PyMuPDF."""

import hashlib
import os
import time

import pytest

pymupdf = pytest.importorskip("pymupdf")

import figure_contract as fc
from figure_extract import extract_figures

W, H = 612, 792
BLACK, RED, BLUE = (0, 0, 0), (1, 0, 0), (0, 0, 1)


def _page(doc):
    return doc.new_page(width=W, height=H)


def _figure(page, rect, fill=RED):
    """A boxed 'plot': filled frame plus a few inner lines."""
    r = pymupdf.Rect(rect)
    page.draw_rect(r, color=BLACK, fill=fill)
    for k in range(1, 4):
        y = r.y0 + k * r.height / 4
        page.draw_line((r.x0 + 5, y), (r.x1 - 5, y), color=BLACK)


def _image(page, rect, rgb=(30, 160, 60)):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 90), False)
    pix.set_rect(pix.irect, rgb)
    page.insert_image(pymupdf.Rect(rect), pixmap=pix, keep_proportion=False)


def _text(page, x, y, text, size=9):
    page.insert_text((x, y), text, fontsize=size)


def _box_text(page, rect, text, size=9):
    assert page.insert_textbox(pymupdf.Rect(rect), text, fontsize=size) >= 0


def _body(page, y=600):
    _box_text(page, (72, y, 540, y + 80), "Body text of the paper. " * 12)


def _save(doc, tmp_path, name="paper.pdf", **kw):
    p = tmp_path / name
    doc.save(p, **kw)
    return p


def _run(pdf, tmp_path, sub="crops", **kw):
    return extract_figures(pdf, tmp_path / "out" / sub, **kw)


def _near(a, b, tol=6):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _simple(tmp_path, name="paper.pdf"):
    doc = pymupdf.open()
    page = _page(doc)
    _figure(page, (100, 100, 400, 300))
    _text(page, 100, 320, "Figure 1. A red plot of expression.")
    _body(page)
    return _save(doc, tmp_path, name)


def test_single_figure_above_caption(tmp_path):
    pdf = _simple(tmp_path)
    res = _run(pdf, tmp_path)
    assert res["status"] == "ok", res
    (f,) = res["figures"]
    assert _near(f["bbox_pt"], (100, 100, 400, 300))
    assert (f["page"], f["page_index"]) == (1, 0)
    assert f["method"] == "caption_anchored_union"
    assert f["figure_label"] == {"value": "Figure 1", "status": "from_caption"}
    assert f["panel_label"] == {"value": None, "status": "not_attempted"}
    assert f["figure_number"] == "1" and f["supplementary"] is False
    assert f["caption"].startswith("Figure 1. A red plot")
    assert f["coord"] == fc.COORD and f["page_size_pt"] == [W, H]
    assert f["pdf_sha256"] == res["pdf_sha256"] == fc.sha256_file(pdf)
    path = tmp_path / "out" / f["file"]
    assert fc.CROP_NAME_RE.match(path.name) and path.exists()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == f["sha256"]
    assert f["media_type"] == fc.MEDIA_TYPES[path.suffix[1:]]
    assert max(f["width_px"], f["height_px"]) <= 1568
    # stable id, cached file reused
    mtime = path.stat().st_mtime_ns
    again = _run(pdf, tmp_path)["figures"][0]
    assert again == f and path.stat().st_mtime_ns == mtime
    assert _run(pdf, tmp_path, sub="other")["figures"][0]["crop_id"] == f["crop_id"]


def test_composite_image_and_chart_is_one_figure(tmp_path):
    doc = pymupdf.open()
    page = _page(doc)
    _image(page, (72, 100, 272, 260))
    page.draw_line((300, 260), (540, 260), color=BLACK)  # x axis
    page.draw_line((300, 100), (300, 260), color=BLACK)  # y axis
    for k, top in enumerate((200, 150, 120, 180)):
        page.draw_rect((310 + 55 * k, top, 350 + 55 * k, 260), color=BLACK, fill=BLUE)
    _box_text(page, (72, 275, 540, 320), "Figure 2. Image and bar chart. " * 3)
    res = _run(_save(doc, tmp_path), tmp_path)
    (f,) = res["figures"]
    x0, y0, x1, y1 = f["bbox_pt"]
    assert x0 <= 72 and x1 >= 540 and y0 <= 100 and y1 >= 260


def test_two_columns(tmp_path):
    doc = pymupdf.open()
    page = _page(doc)
    _figure(page, (60, 100, 280, 300))
    _figure(page, (332, 100, 552, 300), fill=BLUE)
    _text(page, 60, 320, "Figure 1. Left column.")
    _text(page, 332, 320, "Figure 2. Right column.")
    figs = _run(_save(doc, tmp_path), tmp_path)["figures"]
    by = {f["figure_label"]["value"]: f["bbox_pt"] for f in figs}
    assert len(figs) == 2
    assert _near(by["Figure 1"], (60, 100, 280, 300))
    assert _near(by["Figure 2"], (332, 100, 552, 300))


def test_full_width_figure(tmp_path):
    doc = pymupdf.open()
    page = _page(doc)
    _figure(page, (50, 100, 562, 350))
    _box_text(page, (50, 365, 562, 420), "Figure 3. A full-width panel. " * 5)
    (f,) = _run(_save(doc, tmp_path), tmp_path)["figures"]
    assert _near(f["bbox_pt"], (50, 100, 562, 350))


def test_figure_below_caption(tmp_path):
    doc = pymupdf.open()
    page = _page(doc)
    _text(page, 100, 110, "Fig. S2: Supplementary plot.")
    _figure(page, (100, 130, 400, 330))
    _body(page)
    (f,) = _run(_save(doc, tmp_path), tmp_path)["figures"]
    assert _near(f["bbox_pt"], (100, 130, 400, 330))
    assert f["figure_label"]["value"] == "Fig. S2"
    assert f["figure_number"] == "S2" and f["supplementary"] is True


def test_mentions_and_tables_are_not_captions(tmp_path):
    doc = pymupdf.open()
    page = _page(doc)
    _figure(page, (100, 100, 400, 250))
    _text(page, 100, 270, "see Figure 2 for details of the assay.")
    _figure(page, (100, 320, 400, 470))
    _text(page, 100, 490, "Figure 2 shows that the knockdown worked.")
    _figure(page, (100, 540, 400, 690))
    _text(page, 100, 710, "Table 1. Counts per condition.")
    res = _run(_save(doc, tmp_path), tmp_path)
    assert res["status"] == "no_figures" and res["figures"] == []


def test_uncaptioned_graphic(tmp_path):
    doc = pymupdf.open()
    page = _page(doc)
    _figure(page, (100, 100, 400, 300))
    _body(page, 320)
    assert _run(_save(doc, tmp_path), tmp_path)["status"] == "no_figures"


def test_scanned_page(tmp_path):
    doc = pymupdf.open()
    _image(_page(doc), (0, 0, W, H))
    res = _run(_save(doc, tmp_path), tmp_path)
    assert res["status"] == "no_figures" and "scanned_page=1" in res["reason"]


def test_text_only(tmp_path):
    doc = pymupdf.open()
    _body(_page(doc))
    res = _run(_save(doc, tmp_path), tmp_path)
    assert res["status"] == "no_figures" and res["page_count"] == 1


def test_bad_inputs(tmp_path):
    garbage = tmp_path / "garbage.pdf"
    garbage.write_bytes(os.urandom(2000))
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7\n" + os.urandom(500) + b"\n%%EOF\n")
    doc = pymupdf.open()
    _figure(_page(doc), (100, 100, 300, 300))
    enc = _save(
        doc,
        tmp_path,
        "enc.pdf",
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="o",
        user_pw="u",
    )
    link = tmp_path / "link.pdf"
    link.symlink_to(_simple(tmp_path))
    for p, why in [
        (garbage, "not a PDF"),
        (broken, ""),
        (enc, "encrypted"),
        (link, "symlink"),
        (tmp_path / "missing.pdf", "unreadable"),
        (tmp_path, ""),
    ]:
        res = _run(p, tmp_path)
        assert res["status"] == "extraction_error" and res["figures"] == [], (p, res)
        assert why in res["reason"]


def test_too_large(tmp_path):
    res = _run(_simple(tmp_path), tmp_path, max_file_mb=0.001)
    assert res["status"] == "extraction_error" and "too large" in res["reason"]


def test_page_cap(tmp_path):
    doc = pymupdf.open()
    for i in range(60):
        page = _page(doc)
        if i == 49:
            _figure(page, (100, 100, 400, 300))
            _text(page, 100, 320, "Figure 4. Late figure.")
    pdf = _save(doc, tmp_path)
    res = _run(pdf, tmp_path, max_pages=40)
    assert res["status"] == "no_figures" and "pages_not_scanned=20" in res["reason"]
    assert _run(pdf, tmp_path, max_pages=60)["figures"][0]["page"] == 50


def test_heavy_vector_page(tmp_path):
    doc = pymupdf.open()
    page = _page(doc)
    shape = page.new_shape()
    for i in range(20000):
        x, y = 50 + (i % 200) * 2.5, 100 + (i // 200) * 2.5
        shape.draw_rect((x, y, x + 1, y + 1))
        shape.finish(fill=BLACK, color=None)
    shape.commit()
    _text(page, 100, 380, "Figure 1. Dense scatter.")
    t = time.monotonic()
    res = _run(_save(doc, tmp_path), tmp_path, timeout_s=60)
    assert time.monotonic() - t < 30
    assert res["status"] == "ok" and "heavy_vector_page=1" in res["reason"]
    (f,) = res["figures"]  # clustering skipped: the band fallback covers the whole plot
    assert f["method"] == "caption_anchored_band"
    assert _near(f["bbox_pt"], (50, 100, 550.5, 348.5))


def test_rotated_page(tmp_path):
    doc = pymupdf.open()
    page = _page(doc)
    _figure(page, (100, 100, 400, 250))
    _text(page, 100, 270, "Figure 1. Rotated page.")
    page.set_rotation(90)
    (f,) = _run(_save(doc, tmp_path), tmp_path)["figures"]
    assert _near(f["bbox_pt"], (100, 100, 400, 250))  # unrotated page space
    assert f["page_size_pt"] == [W, H]
    assert f["height_px"] > f["width_px"]  # rendered as displayed (turned 90 degrees)
    pix = pymupdf.Pixmap(str(tmp_path / "out" / f["file"]))
    red = [
        pix.pixel(x, y) for x in range(0, pix.width, 7) for y in range(0, pix.height, 7)
    ]
    assert sum(p[0] > 200 and p[1] < 60 for p in red) > 0.5 * len(
        red
    )  # red plot, not blank


def test_overrides(tmp_path):
    pdf = _simple(tmp_path)
    manual = {"page": 1, "bbox_pt": [90, 90, 410, 310], "figure_label": "Figure 1"}
    (f,) = _run(pdf, tmp_path, overrides=[manual])["figures"]
    assert f["method"] == "manual_override" and f["bbox_pt"] == [90, 90, 410, 310]
    assert f["figure_label"] == {"value": "Figure 1", "status": "manual"}
    assert f["caption"].startswith("Figure 1. A red plot")

    res = _run(
        pdf, tmp_path, overrides=[{"page": 1, "exclude": True, "figure_label": "Fig 1"}]
    )
    assert res["status"] == "no_figures"

    bad = [
        {"page": 9, "bbox_pt": [0, 0, 10, 10]},
        {"page": 1, "bbox_pt": [0, 0, float("nan"), 10]},
        {"page": 1, "bbox_pt": [0, 0, 10, 900]},
        {"page": 1, "exclude": True, "figure_label": "x" * 41},
        {"page": True, "exclude": True},
        {"page": 1, "exclude": True, "colour": "red"},
        "nonsense",
    ]
    res = _run(pdf, tmp_path, overrides=bad)
    assert len(res["override_warnings"]) == len(bad)
    assert len(res["figures"]) == 1 and "override_warnings=7" in res["reason"]


@pytest.mark.parametrize(
    "caption", ["Figure 3 (continued)", "Figure 5. Cont.", "FIGURE 2 Continued"]
)
def test_continued_caption(tmp_path, caption):
    doc = pymupdf.open()
    page = _page(doc)
    _figure(page, (100, 100, 400, 300))
    _text(page, 100, 320, caption)
    (f,) = _run(_save(doc, tmp_path), tmp_path)["figures"]
    assert f["figure_label"]["status"] == "uncertain" and f["partial"] is True


def test_caption_truncation(tmp_path):
    doc = pymupdf.open()
    page = _page(doc)
    _figure(page, (100, 100, 400, 300))
    _box_text(
        page, (72, 320, 540, 700), "Figure 1. " + "long legend words " * 120, size=6
    )
    (f,) = _run(_save(doc, tmp_path), tmp_path)["figures"]
    assert len(f["caption"]) == 1500 and f["caption_truncated"] is True


def test_timeout(tmp_path):
    res = _run(_simple(tmp_path), tmp_path, timeout_s=0.01)
    assert res["status"] == "extraction_error" and res["reason"] == "timeout"


def test_pointer_caption_takes_next_page_legend(tmp_path):
    doc = pymupdf.open()
    page = _page(doc)
    _figure(page, (100, 100, 400, 300))
    _text(page, 100, 320, "FIGURE 2 | Legend on next page.")
    page = _page(doc)
    legend = "FIGURE 2 | The real legend of the figure. " * 6
    _box_text(page, (72, 60, 540, 140), legend)
    _body(page, 200)
    res = _run(_save(doc, tmp_path), tmp_path)
    (f,) = res["figures"]
    assert f["page"] == 1 and f["caption_source"] == "next_page"
    assert f["caption"].startswith("FIGURE 2 | The real legend")
    assert (
        "unplaced" not in res["reason"]
    )  # the legend block on page 2 is accounted for


def test_caption_hidden_by_empty_actualtext(tmp_path):
    doc = pymupdf.open()
    page = _page(doc)
    _figure(page, (100, 100, 400, 300))
    _text(page, 100, 320, "Figure 1. Caption tagged with an empty ActualText.")
    page.clean_contents()
    xref = page.get_contents()[0]
    c = doc.xref_stream(xref)
    i, j = c.rfind(b"BT"), c.rfind(b"ET") + 2
    c = c[:i] + b"/Span <</ActualText ()>> BDC\n" + c[i:j] + b"\nEMC" + c[j:]
    doc.update_stream(xref, c)
    (f,) = _run(_save(doc, tmp_path), tmp_path)["figures"]
    assert f["caption"].startswith("Figure 1. Caption tagged")
    assert f["caption_source"] == "same_page"


def test_band_for_full_width_figure_with_column_caption(tmp_path):
    """Full-width figure in two separate halves; caption in the left column, its continuation in the right."""
    doc = pymupdf.open()
    page = _page(doc)
    _box_text(
        page, (50, 50, 560, 120), "Body text of the paper above the figure. " * 12
    )
    _figure(page, (50, 150, 290, 450))
    _figure(page, (320, 150, 560, 450), fill=BLUE)
    _box_text(
        page, (50, 470, 290, 560), "Fig. 1 | Two-part figure. " + "Legend words. " * 20
    )
    _box_text(page, (320, 470, 560, 560), "legend continued in the right column. " * 8)
    (f,) = _run(_save(doc, tmp_path), tmp_path)["figures"]
    assert f["method"] == "caption_anchored_band"
    x0, y0, x1, y1 = f["bbox_pt"]
    assert x0 <= 50 and x1 >= 560 and y1 <= 470
    assert y0 >= 120  # never body text


def test_unplaced_caption_in_reason(tmp_path):
    doc = pymupdf.open()
    page = _page(doc)
    _figure(page, (100, 100, 400, 300))
    _text(page, 100, 320, "Figure 1. Placed.")
    _body(page, 360)
    _text(page, 100, 500, "Figure 7. A caption with no artwork near it.")
    _body(page, 520)
    res = _run(_save(doc, tmp_path), tmp_path)
    assert len(res["figures"]) == 1 and "unplaced=Figure 7" in res["reason"]


def test_extended_data_label(tmp_path):
    doc = pymupdf.open()
    page = _page(doc)
    _figure(page, (100, 100, 400, 300))
    _text(page, 100, 320, "Extended Data Fig. 3 | Quality control.")
    (f,) = _run(_save(doc, tmp_path), tmp_path)["figures"]
    assert f["figure_label"]["value"] == "Extended Data Fig. 3"
    assert f["figure_number"] == "ED3" and f["supplementary"] is False


@pytest.mark.parametrize("damage", ["truncated", "empty"])
def test_broken_cached_crop_is_rendered_again(tmp_path, damage):
    pdf = _simple(tmp_path)
    good = _run(pdf, tmp_path, sub="first")["figures"][0]
    blob = (tmp_path / "out" / good["file"]).read_bytes()
    crops = tmp_path / "out" / "crops"
    crops.mkdir(parents=True)
    name = good["file"].split("/")[-1]
    (crops / name).write_bytes(blob[: len(blob) // 2] if damage == "truncated" else b"")
    res = _run(pdf, tmp_path)
    assert res["status"] == "ok", res
    (f,) = res["figures"]
    path = tmp_path / "out" / f["file"]
    assert f["crop_id"] == good["crop_id"] and f["sha256"] == good["sha256"]
    assert (
        path.read_bytes() == blob and pymupdf.Pixmap(str(path)).width == f["width_px"]
    )
    assert sorted(p.name for p in crops.iterdir()) == [name]  # no temp files left
