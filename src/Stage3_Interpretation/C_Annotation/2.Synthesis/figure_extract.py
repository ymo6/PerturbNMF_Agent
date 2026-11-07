"""Crop the captioned figures of a paper PDF for the figure-evidence step.

    from figure_extract import extract_figures
    res = extract_figures(pdf_path, crops_dir)  # {"status", "reason", "pdf_sha256", "page_count", "figures"}

    python figure_extract.py <pdf> <crops_dir>   # prints the result as JSON

A figure is anchored on a caption block whose first line starts "Figure N" / "Fig. N" (also "Extended Data Fig. N",
"Supplementary Figure N"; figure_number "3", "S2", "ED1"). Its region is the union of the embedded images and
vector-drawing clusters in the caption's column, above the caption (else below it), plus nearby panel letters and axis
labels (method caption_anchored_union). The band fallback (caption_anchored_band) takes everything between the caption
and the nearest body text, caption or header strip instead, over the caption's column or the full content width when
no body text or side-by-side caption is in the way; it is used when the page has too many drawings to cluster or the
union covers < 90% of the band. A band never holds body text. Graphics with no caption are never figures; neither are
tables or scanned pages. A pointer caption ("Legend on next page.", or < 40 chars after its label) takes its text from
the next page's caption block with the same label (caption_source "next_page"). Continuations ("(continued)",
"Cont.") get partial: true and label status uncertain. Captions left without a region are listed in reason as
"unplaced=<label>,...".

Each figure is rendered once into crops_dir/<crop_id>.png|.jpg (whichever is smaller; an existing crop with the same
id is reused) and described by the extractor-owned FIGURE fields of figure_contract.py plus "figure_number",
"supplementary" and "caption_source". Coordinates are PDF points on the unrotated page, origin top-left, y down
(figure_contract.COORD).

The PDF is read once by this process (regular file, no symlink, size cap, %PDF- magic) and parsed and rendered by a
child process with CPU-time, page and wall-clock limits and a 2 GiB address-space limit. macOS refuses RLIMIT_AS, so
there memory is bounded only by the 12-megapixel render cap and the wall-clock timeout. Crops are written to a temp
file and renamed into place; a cached crop that does not decode is rendered again. extract_figures never raises: any
failure is {"status": "extraction_error", "reason": ...}.

overrides: [{"page": 1-based, "bbox_pt": [x0, y0, x1, y1], "figure_label": "Figure 2", "exclude": bool}, ...]. A
bbox adds a manual figure (method manual_override, label status manual; it replaces a detected figure with the same
label on that page); exclude drops the detected figures of that page (only the one with figure_label, if given).
Invalid entries are ignored and listed in "override_warnings".
"""

import json
import math
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
if (
    str(HERE) not in sys.path
):  # the worker runs with -I, which leaves the script folder off sys.path
    sys.path.insert(0, str(HERE))
import figure_contract as fc

CAPTION_RE = re.compile(
    r"^\s*((?:Extended\s+Data\s+|Supplementary\s+)?(?:Figure|Fig\.?))\s*(S?\d+[A-Za-z]?)\b",
    re.IGNORECASE,
)
TABLE_RE = re.compile(r"^\s*Table\s*S?\d+", re.IGNORECASE)
CONTINUED_RE = re.compile(r"^\W*cont(\.|inued\b|\b)", re.IGNORECASE)
POINTER_RE = re.compile(
    r"legend (is )?on (the )?(next|following) page|see (the )?next page", re.IGNORECASE
)
MAX_CAPTION = 1500
MAX_PIXELS = 12_000_000
MAX_DRAWINGS = 5000
MAX_LABEL = 40
WORKER_MEMORY = 2 * 1024**3
CROP_MAGIC = {
    "png": (b"\x89PNG\r\n\x1a\n", b"IEND\xaeB`\x82"),
    "jpg": (b"\xff\xd8\xff", b"\xff\xd9"),
}
OVERRIDE_KEYS = {"page", "bbox_pt", "figure_label", "exclude"}


def _error(reason, pdf_sha256=None, page_count=None, **extra):
    return {
        "status": "extraction_error",
        "reason": reason,
        "pdf_sha256": pdf_sha256,
        "page_count": page_count,
        "figures": [],
        **extra,
    }


def extract_figures(
    pdf_path,
    crops_dir,
    *,
    overrides=None,
    max_pages=40,
    max_file_mb=60,
    timeout_s=90,
    max_long_side=1568,
    min_dpi=100,
    max_dpi=220,
):
    """Crop the captioned figures of pdf_path into crops_dir (see the module docstring). max_file_mb is in MiB
    (2**20 bytes). Never raises."""
    try:
        return _extract(
            pdf_path,
            crops_dir,
            overrides,
            max_pages,
            max_file_mb,
            timeout_s,
            max_long_side,
            min_dpi,
            max_dpi,
        )
    except Exception as e:  # noqa: BLE001 - never raise to the caller
        return _error(f"unexpected error: {type(e).__name__}")


def _read_pdf(pdf_path, max_file_mb):
    """(bytes, None) for a regular, non-symlink file under the size cap that starts with %PDF-; else (None, why)."""
    try:
        fd = os.open(pdf_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        return None, "symlink" if os.path.islink(
            pdf_path
        ) else f"unreadable: {e.strerror}"
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            return None, "not a regular file"
        if st.st_size > max_file_mb * 1024 * 1024:
            return None, f"too large (> {max_file_mb} MB)"
        data = f.read(int(max_file_mb * 1024 * 1024) + 1)
    if len(data) > max_file_mb * 1024 * 1024:
        return None, f"too large (> {max_file_mb} MB)"
    if not data.startswith(b"%PDF-"):
        return None, "not a PDF"
    return data, None


def _extract(
    pdf_path, crops_dir, overrides, max_pages, max_file_mb, timeout_s, *dpi_args
):
    data, why = _read_pdf(pdf_path, max_file_mb)
    if why:
        return _error(why)
    pdf_sha = fc.sha256_bytes(data)
    crops = Path(crops_dir)
    if crops.is_symlink():
        return _error("crops_dir is a symlink", pdf_sha)
    crops.mkdir(parents=True, exist_ok=True)
    args = {
        "crops_dir": str(crops.resolve()),
        "pdf_sha256": pdf_sha,
        "overrides": overrides,
        "max_pages": int(max_pages),
        "cpu_s": max(1, math.ceil(timeout_s)),
        "dpi_args": [int(v) for v in dpi_args],
    }
    cmd = [sys.executable, "-I", str(Path(__file__).resolve()), "--worker"]
    cmd.append(json.dumps(args, default=lambda o: None))
    # HiveBlot principle reuse (api/app/experimental/uploads.py:_INSPECT @727b4f0): parse untrusted PDFs only in a
    # child "python -I" with a minimal environment and a wall-clock timeout. New code.
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": os.environ.get("PATH", "")},
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(data, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        return _error("timeout", pdf_sha)
    lines = out.decode("utf-8", "replace").strip().splitlines()
    try:
        res = json.loads(lines[-1])
    except (IndexError, ValueError):
        res = None
    if not isinstance(res, dict) or res.get("status") not in (
        "ok",
        "no_figures",
        "extraction_error",
    ):
        if proc.returncode == -getattr(signal, "SIGXCPU", -1):
            return _error("timeout (cpu limit)", pdf_sha)
        tail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        return _error(
            f"worker failed (exit {proc.returncode}) {tail[0][:200]}".strip(), pdf_sha
        )
    res["pdf_sha256"] = pdf_sha
    return res


# ---- worker (runs in the child process) ----


def _limit_resources(cpu_s):
    # HiveBlot principle reuse (api/app/experimental/uploads.py:_INSPECT @727b4f0): cap the parser child's CPU time
    # and address space; macOS refuses RLIMIT_AS, so only there is a refusal ignored. New code.
    # On macOS memory is therefore bounded only by MAX_PIXELS per render and the parent's wall-clock timeout.
    import resource

    for which, val in (
        (resource.RLIMIT_CPU, cpu_s),
        (resource.RLIMIT_AS, WORKER_MEMORY),
    ):
        try:
            hard = resource.getrlimit(which)[1]
            val = val if hard == resource.RLIM_INFINITY else min(val, hard)
            resource.setrlimit(which, (val, val))
        except (ValueError, OSError):
            if sys.platform != "darwin":
                raise


def _label_key(s):
    return re.sub(r"[\s.]", "", str(s or "").lower()).replace("figure", "fig")


def _check_overrides(raw, doc):
    """(valid overrides, warnings)."""
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], ["overrides is not a list"]
    good, warns = [], []
    for i, o in enumerate(raw):
        why = _override_problem(o, doc)
        if why:
            warns.append(f"override {i}: {why}")
        else:
            good.append(o)
    return good, warns


def _override_problem(o, doc):
    if not isinstance(o, dict):
        return "not an object"
    if set(o) - OVERRIDE_KEYS:
        return f"unknown keys {sorted(set(o) - OVERRIDE_KEYS)}"
    page = o.get("page")
    if type(page) is not int or not 1 <= page <= doc.page_count:
        return "page must be an int in 1..page_count"
    if "exclude" in o and type(o["exclude"]) is not bool:
        return "exclude must be a bool"
    label = o.get("figure_label")
    if "figure_label" in o and (
        not isinstance(label, str) or not 0 < len(label.strip()) <= MAX_LABEL
    ):
        return f"figure_label must be a non-empty string of <= {MAX_LABEL} chars"
    if o.get("exclude"):
        return "exclude cannot carry bbox_pt" if "bbox_pt" in o else None
    if "bbox_pt" not in o:
        return "needs bbox_pt or exclude"
    b = o["bbox_pt"]
    if not (
        isinstance(b, list)
        and len(b) == 4
        and all(type(v) in (int, float) and math.isfinite(v) for v in b)
    ):
        return "bbox_pt must be 4 finite numbers"
    page_obj = doc[page - 1]
    pr = page_obj.rect * page_obj.derotation_matrix
    if not (pr.x0 <= b[0] < b[2] <= pr.x1 and pr.y0 <= b[1] < b[3] <= pr.y1):
        return "bbox_pt must be a non-empty box inside the page"
    return None


def _split_columns(block):
    """[(rect, lines)] of one text block, split where it joins side-by-side columns (MuPDF can put two columns'
    captions on one baseline into one block)."""
    import pymupdf

    groups = []
    for ln in block.get("lines", []):
        segs = []
        for s in ln["spans"]:
            r = pymupdf.Rect(s["bbox"])
            if segs and r.x0 - segs[-1][0].x1 <= 2 * max(s["size"], 1):
                segs[-1][0] |= r
                segs[-1][1] += s["text"]
            else:
                segs.append([r, s["text"]])
        for r, t in segs:
            g = next(
                (g for g in groups if _overlap(g[0].x0, g[0].x1, r.x0, r.x1) > 0), None
            )
            if g:
                g[0] |= r
                g[1].append(t)
            else:
                groups.append([r, [t]])
    return groups


def _page_text(page):
    """Text blocks of a page: [{"rect", "lines", "text"}] (ActualText ignored: some PDFs blank their captions
    with an empty ActualText)."""
    import pymupdf

    flags = pymupdf.TEXTFLAGS_TEXT | pymupdf.TEXT_IGNORE_ACTUALTEXT
    out = []
    for b in page.get_text("dict", flags=flags)["blocks"]:
        for rect, lines in _split_columns(b):
            lines = [ln.strip() for ln in lines if ln.strip()]
            if lines:
                text = re.sub(r"\s+", " ", " ".join(lines)).strip()
                out.append({"rect": rect, "lines": lines, "text": text})
    return out


def _figure_number(m):
    """'3', 'S2' (supplementary) or 'ED1' (extended data) from a CAPTION_RE match."""
    num = re.match(r"S?\d+", m.group(2), re.IGNORECASE).group(0).upper()
    prefix = m.group(1).lower()
    if prefix.startswith("extended"):
        return "ED" + num.lstrip("S")
    if prefix.startswith("supplementary") and not num.startswith("S"):
        return "S" + num
    return num


def _caption_blocks(blocks):
    """(figure captions, table caption rects) among the text blocks."""
    caps, tables = [], []
    for b in blocks:
        first, text = b["lines"][0], b["text"]
        if TABLE_RE.match(first):
            tables.append(b["rect"])
            continue
        m = CAPTION_RE.match(first)
        if not m:
            continue
        rest = first[m.end() :]
        if re.match(r"\s*[a-z]", rest) and not CONTINUED_RE.match(rest):
            continue  # "Figure 2 shows ..." is body text, not a caption
        label = first[m.start(1) : m.end(2)]
        caps.append(
            {
                "rect": b["rect"],
                "label": label,
                "number": _figure_number(m),
                "partial": bool(CONTINUED_RE.match(rest)),
                "pointer": bool(POINTER_RE.search(text)) or len(text) - len(label) < 40,
                "caption": text[:MAX_CAPTION],
                "truncated": len(text) > MAX_CAPTION,
                "caption_source": "same_page",
            }
        )
    return caps, tables


def _is_strip(r, pr):
    """Thin header/footer strip: a wide box in the top/bottom 5% or a rule in the top/bottom 8%."""
    h = pr.height

    def edge(f):
        return r.y1 <= pr.y0 + f * h or r.y0 >= pr.y1 - f * h

    return (edge(0.05) and r.width > 8 * max(r.height, 0.1)) or (
        edge(0.08) and r.height <= 3
    )


def _graphics(page, pr, notes):
    """(figure-sized image and vector-cluster boxes, every raw graphic box, heavy-vector page?)."""
    import pymupdf

    area, h = pr.width * pr.height, pr.height
    images = [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()]
    drawings = page.get_drawings()
    heavy = len(drawings) > MAX_DRAWINGS
    boxes = list(images)
    if heavy:
        notes["heavy_vector_page"] += (
            1  # no clustering; the band fallback covers the figure
        )
    elif drawings:
        boxes += page.cluster_drawings(drawings=drawings)
    keep = []
    for r in boxes:
        r = r & pr
        a = r.width * r.height
        if r.is_empty or a < 0.01 * area or a >= 0.85 * area or r.height < 0.03 * h:
            continue
        if not _is_strip(r, pr):
            keep.append(r)
    raw = []
    for r in images + [d["rect"] for d in drawings]:
        r = pymupdf.Rect(r.x0 - 0.5, r.y0 - 0.5, r.x1 + 0.5, r.y1 + 0.5) & pr
        if not r.is_empty and r.get_area() < 0.85 * area and not _is_strip(r, pr):
            raw.append(r)
    return keep, raw, heavy


def _overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def _union(rects):
    import pymupdf

    u = pymupdf.Rect(rects[0])
    for r in rects[1:]:
        u |= r
    return u


class _Page:
    """Layout facts of one page shared by its captions."""

    def __init__(self, page, pr, notes):
        self.pr = pr
        self.blocks = _page_text(page)
        self.caps, self.tables = _caption_blocks(self.blocks)
        self.n_chars = sum(len(b["text"]) for b in self.blocks)
        self.page, self.notes = page, notes

    def load_graphics(self):
        self.graphics, self.raw, self.heavy = _graphics(self.page, self.pr, self.notes)
        pr, h = self.pr, self.pr.height
        head = [b["rect"].y1 for b in self.blocks if b["rect"].y1 <= pr.y0 + 0.06 * h]
        foot = [b["rect"].y0 for b in self.blocks if b["rect"].y0 >= pr.y1 - 0.06 * h]
        self.top, self.bottom = max(head, default=pr.y0), min(foot, default=pr.y1)
        xs = [b["rect"] for b in self.blocks] + self.raw
        self.x0 = max(pr.x0, min((r.x0 for r in xs), default=pr.x0))
        self.x1 = min(pr.x1, max((r.x1 for r in xs), default=pr.x1))
        self.cap_rects = [c["rect"] for c in self.caps] + self.tables

    def body(self, x0, x1):
        """Body-text rects for an x-span: >= 200 chars, >= 40% of the span wide, not captions."""
        return [
            b["rect"]
            for b in self.blocks
            if len(b["text"]) >= 200
            and b["rect"].width >= 0.4 * (x1 - x0)
            and _overlap(b["rect"].x0, b["rect"].x1, x0, x1) > 0
            and b["rect"] not in self.cap_rects
        ]

    def limits(self, cb, x0, x1, side):
        """Nearest caption, table or body text edge beyond cb on one side, within the x-span."""
        stops = [
            r
            for r in self.cap_rects + self.body(x0, x1)
            if r != cb and _overlap(r.x0, r.x1, x0, x1) > 0
        ]
        if side == "above":
            return max([self.top] + [r.y1 for r in stops if r.y1 <= cb.y0 + 2])
        return min([self.bottom] + [r.y0 for r in stops if r.y0 >= cb.y1 - 2])

    def band(self, cb, x0, x1, side):
        """(tightened band, band region) between cb and the nearest stop, or None when it holds body text or no
        graphic."""
        import pymupdf

        lim = self.limits(cb, x0, x1, side)
        if side == "above":
            region = pymupdf.Rect(x0, lim, x1, cb.y0)
        else:
            region = pymupdf.Rect(x0, cb.y1, x1, lim)
        if region.height < 10:
            return None
        for r in self.body(x0, x1):
            ov = r & region
            if not ov.is_empty and ov.width > 3 and ov.height > 3:
                return None  # never put body text in a band

        def inside(r):
            return region.contains((r.tl + r.br) / 2)

        art = [r for r in self.raw if inside(r)]
        if not art:
            return None
        labels = [b["rect"] for b in self.blocks if inside(b["rect"])]
        return _union(art + labels) & region, region


def _detect(page, pr, notes):
    """(caption-anchored figure regions [{"rect", "method", caption fields}], captions without a region)."""
    import pymupdf

    pg = _Page(page, pr, notes)
    area = pr.width * pr.height
    if pg.n_chars < 20 and any(
        (pymupdf.Rect(i["bbox"]) & pr).get_area() >= 0.85 * area
        for i in page.get_image_info()
    ):
        notes["scanned_page"] += 1
        return [], []
    if not pg.caps:
        return [], []
    pg.load_graphics()
    graphics, mid = pg.graphics, pr.x0 + pr.width / 2
    claimed, found, unplaced = set(), [], []
    for c in sorted(pg.caps, key=lambda c: (c["rect"].y0, c["rect"].x0)):
        cb = c["rect"]
        full = cb.x0 < mid < cb.x1

        def in_column(g, cb=cb, full=full):
            ov = _overlap(g.x0, g.x1, cb.x0, cb.x1)
            return ov >= 0.3 * min(g.width, cb.width) or (full and g.x0 < mid < g.x1)

        top = pg.limits(cb, cb.x0, cb.x1, "above")
        bottom = pg.limits(cb, cb.x0, cb.x1, "below")
        cand = [i for i, g in enumerate(graphics) if i not in claimed and in_column(g)]
        side = "above"
        pick = [
            i for i in cand if graphics[i].y1 <= cb.y0 + 2 and graphics[i].y0 >= top - 2
        ]
        if not pick:
            side = "below"
            pick = [
                i
                for i in cand
                if graphics[i].y0 >= cb.y1 - 2 and graphics[i].y1 <= bottom + 2
            ]
        union = _union([graphics[i] for i in pick]) if pick else None
        # band fallback: the caption's column, or the content width when nothing (body text, a side-by-side
        # caption) stands in the way
        side_caps = [
            o["rect"]
            for o in pg.caps
            if o is not c and _overlap(o["rect"].y0, o["rect"].y1, cb.y0, cb.y1) > 0
        ]
        band = area_ = None
        for s in [side] if pick else ["above", "below"]:
            col = pg.band(cb, cb.x0, cb.x1, s)
            wide = None if side_caps else pg.band(cb, pg.x0, pg.x1, s)
            if wide and (not col or wide[1].height >= 0.8 * col[1].height):
                band, area_ = wide
            elif col:
                band, area_ = col
            if band:
                break
        if union and area_:  # add panel letters and axis labels just outside the art
            near = pymupdf.Rect(
                union.x0 - 15, union.y0 - 15, union.x1 + 15, union.y1 + 15
            )
            for b in pg.blocks:
                r = b["rect"]
                if area_.contains((r.tl + r.br) / 2) and r.intersects(near):
                    union |= r & area_
        if band and (
            union is None or pg.heavy or union.get_area() < 0.9 * band.get_area()
        ):
            region, method = band, "caption_anchored_band"
            claimed.update(
                i for i, g in enumerate(graphics) if band.contains((g.tl + g.br) / 2)
            )
        elif union:
            region, method = union, "caption_anchored_union"
            claimed.update(pick)
        else:
            unplaced.append(c)
            continue
        u = (
            pymupdf.Rect(region.x0 - 4, region.y0 - 4, region.x1 + 4, region.y1 + 4)
            & pr
        )
        ratio = u.width / u.height if u.height else math.inf
        if (
            ratio > 6
            or ratio < 1 / 6
            or u.get_area() < 0.02 * area
            or (not full and u.width < 0.15 * cb.width)
        ):
            notes["rejected_region"] += 1
            unplaced.append(c)
            continue
        found.append({**c, "rect": u, "method": method})
    return found, unplaced


# HiveBlot principle reuse (western_blot_miner/pdf_preprocess.py:load_for_vlm @727b4f0): cap the long side of an image
# meant for a vision model; here by choosing the render dpi instead of resizing. New code.
def _choose_dpi(w, h, max_long_side, min_dpi, max_dpi):
    """dpi giving a long side <= max_long_side px, clamped to [min_dpi, max_dpi], and never above MAX_PIXELS (which
    may push it below min_dpi)."""
    dpi = min(max(max_long_side * 72 / max(w, h), min_dpi), max_dpi)
    dpi = min(dpi, 72 * math.sqrt(MAX_PIXELS / (w * h)))
    return max(1, int(dpi))


def _cached_crop(crops, cid):
    """(ext, bytes, pixmap) of an existing, complete crop with this id, else None. Broken ones (half-written,
    empty) are removed so the crop is rendered again."""
    import pymupdf

    for ext in ("png", "jpg"):
        hit = fc.safe_child(crops, f"{cid}.{ext}")
        if not hit:
            continue
        blob = hit.read_bytes()
        head, tail = CROP_MAGIC[ext]
        if blob.startswith(head) and blob.endswith(tail):
            try:
                pix = pymupdf.Pixmap(blob)
            except Exception:  # noqa: BLE001 - any decode failure means re-render
                pix = None
            if pix and pix.width > 0 and pix.height > 0:
                return ext, blob, pix
        hit.unlink(missing_ok=True)
    return None


def _write_atomic(crops, name, blob):
    """Write crops/name via a temp file in the same folder and os.replace; False on failure."""
    tmp = crops / f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, crops / name)  # replaces a symlink itself, never its target
        return True
    except OSError:
        tmp.unlink(missing_ok=True)
        return False


def _crop(page, pidx, pr, fig, pdf_sha, crops, dpi_args, notes):
    """Render (or reuse) one figure's crop; the FIGURE dict, or None when it cannot be written."""
    import pymupdf

    rect = pymupdf.Rect([round(v, 1) for v in fig["rect"]])
    dpi = _choose_dpi(rect.width, rect.height, *dpi_args)
    cid = fc.crop_id(pdf_sha, pidx, list(rect), dpi)
    cached = _cached_crop(crops, cid)
    if cached:
        ext, blob, pix = cached
    else:
        # clip is in rotated (displayed) space; bbox_pt stays in unrotated space
        pix = page.get_pixmap(clip=rect * page.rotation_matrix, dpi=dpi)
        png, jpg = pix.tobytes("png"), pix.tobytes("jpeg", jpg_quality=85)
        ext, blob = ("png", png) if len(png) <= len(jpg) else ("jpg", jpg)
        if not _write_atomic(crops, f"{cid}.{ext}", blob):
            notes["crop_write_error"] += 1
            return None
    out = {
        "crop_id": cid,
        "file": f"{crops.name}/{cid}.{ext}",
        "sha256": fc.sha256_bytes(blob),
        "media_type": fc.MEDIA_TYPES[ext],
        "width_px": pix.width,
        "height_px": pix.height,
        "pdf_sha256": pdf_sha,
        "page": pidx + 1,
        "page_index": pidx,
        "page_size_pt": [round(pr.width, 2), round(pr.height, 2)],
        "bbox_pt": list(rect),
        "coord": fc.COORD,
        "dpi": dpi,
        "method": fig["method"],
        "figure_label": {"value": fig["label"], "status": fig["label_status"]},
        "panel_label": {"value": None, "status": "not_attempted"},
        "caption": fig["caption"],
        "caption_truncated": fig["truncated"],
        "caption_source": fig.get("caption_source", "same_page"),
        # the figure's own text layer (axis labels, printed p-values); used only by host checks, never shown
        "figure_text": " ".join(page.get_text("text", clip=rect).split())[:4000],
        "figure_number": fig["number"],
        "supplementary": bool(fig["number"] and fig["number"].startswith("S")),
    }
    if fig.get("partial"):
        out["partial"] = True
    return out


def _apply_overrides(found, ovs, pr):
    """Drop excluded figures and add manual ones for one page."""
    import pymupdf

    for o in ovs:
        if o.get("exclude"):
            key = _label_key(o.get("figure_label"))
            found = [f for f in found if key and _label_key(f["label"]) != key]
    for o in ovs:
        if o.get("exclude"):
            continue
        label = o["figure_label"].strip() if o.get("figure_label") else None
        key = _label_key(label)
        same = [f for f in found if key and _label_key(f["label"]) == key]
        found = [f for f in found if f not in same]
        m = CAPTION_RE.match(label or "")
        number = _figure_number(m) if m else None
        found.append(
            {
                "rect": pymupdf.Rect(o["bbox_pt"]) & pr,
                "method": "manual_override",
                "label": label,
                "label_status": "manual",
                "number": number,
                "caption": same[0]["caption"] if same else "",
                "truncated": same[0]["truncated"] if same else False,
            }
        )
    return found


def _next_page_legend(doc, pidx, fig):
    """The full legend of a pointer caption ("Legend on next page."): the next page's caption block with the same
    label, or None."""
    if pidx + 1 >= doc.page_count:
        return None
    caps, _ = _caption_blocks(_page_text(doc[pidx + 1]))
    key = _label_key(fig["label"])
    return next(
        (c for c in caps if _label_key(c["label"]) == key and not c["pointer"]), None
    )


def _worker(args, data):
    _limit_resources(args["cpu_s"])
    import pymupdf

    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    if not hasattr(pymupdf.Page, "cluster_drawings"):
        return _error("PyMuPDF too old (no Page.cluster_drawings)")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:  # noqa: BLE001
        return _error("unreadable PDF")
    # HiveBlot principle reuse (api/app/experimental/uploads.py:_INSPECT @727b4f0): refuse encrypted or page-less
    # PDFs and cap the number of pages parsed. New code.
    if doc.needs_pass or doc.is_encrypted:
        return _error("encrypted")
    n = doc.page_count
    if n == 0:
        return _error("no pages", page_count=0)
    overrides, warns = _check_overrides(args["overrides"], doc)
    by_page = {}
    for o in overrides:
        by_page.setdefault(o["page"] - 1, []).append(o)
    crops = Path(args["crops_dir"])
    notes, figures, unplaced, consumed = Counter(), [], [], set()
    for pidx in sorted(set(range(min(n, args["max_pages"]))) | set(by_page)):
        page = doc[pidx]
        pr = page.rect * page.derotation_matrix
        found, missed = (
            _detect(page, pr, notes) if pidx < args["max_pages"] else ([], [])
        )
        for f in found:
            f["label_status"] = "uncertain" if f["partial"] else "from_caption"
            legend = _next_page_legend(doc, pidx, f) if f["pointer"] else None
            if legend:
                f.update(caption=legend["caption"], truncated=legend["truncated"])
                f["caption_source"] = "next_page"
                consumed.add((pidx + 1, _label_key(f["label"])))
        unplaced += [
            c["label"] for c in missed if (pidx, _label_key(c["label"])) not in consumed
        ]
        found = _apply_overrides(found, by_page.get(pidx, []), pr)
        for f in found:
            fig = _crop(
                page, pidx, pr, f, args["pdf_sha256"], crops, args["dpi_args"], notes
            )
            if fig:
                figures.append(fig)
    if n > args["max_pages"]:
        notes["pages_not_scanned"] = n - args["max_pages"]
    if warns:
        notes["override_warnings"] = len(warns)
    reason = "; ".join(f"{k}={v}" for k, v in sorted(notes.items()))
    if unplaced:
        reason = (reason + "; unplaced=" + ",".join(dict.fromkeys(unplaced))).strip(
            "; "
        )
    if not figures:
        reason = ("no captioned figure found; " + reason).strip("; ")
    return {
        "status": "ok" if figures else "no_figures",
        "reason": reason,
        "pdf_sha256": args["pdf_sha256"],
        "page_count": n,
        "figures": figures,
        "override_warnings": warns,
    }


def _worker_main(arg):
    real_stdout, sys.stdout = (
        sys.stdout,
        sys.stderr,
    )  # stray prints must not corrupt the JSON line
    try:
        res = _worker(json.loads(arg), sys.stdin.buffer.read())
    except MemoryError:
        res = _error("memory limit")
    except Exception as e:  # noqa: BLE001
        res = _error(f"worker error: {type(e).__name__}")
    real_stdout.write(json.dumps(res) + "\n")
    real_stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        _worker_main(sys.argv[2])
    elif len(sys.argv) == 3:
        print(json.dumps(extract_figures(sys.argv[1], sys.argv[2]), indent=1))
    else:
        sys.exit("usage: python figure_extract.py <pdf> <crops_dir>")
