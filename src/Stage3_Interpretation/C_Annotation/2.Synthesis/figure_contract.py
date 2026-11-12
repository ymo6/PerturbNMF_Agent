"""Shared contract of the figure-evidence step (standard library only, so the viewer can import it).

figure_evidence.py writes one record per program, <run_dir>/Synthesis/Figures/P<k>/figure_evidence.json, with its
crops in <run_dir>/Synthesis/Figures/P<k>/crops/. The folder sits outside Programs/P<k>/ on purpose: the annotation
agent can write its own program folder, so nothing the viewer embeds may come from there.

    {"schema_version": 1, "program": "P3", "label": <label.json program_name>,
     "interpretation_sha256": <sha256 of the raw interpretation.json bytes>, "screen_context": "<cell type> (<organism>)",
     "model", "prompts", "created", "extractor_version",
     "items": [ITEM, ...]}

    ITEM = one (interpretation slot, cited paper) pair
    {"item_id": "<slot>:<pmid>", "slot", "slot_claim", "pmid",
     "paper": {"pmid", "doi", "pmcid", "title", "year", "license", "license_source"},
     "sub_claim": what the figure is judged against (the paper's recorded finding, not the slot claim),
     "finding", "paper_context", "genes", "context_match": one of CONTEXT_MATCH,
     "status": one of ITEM_STATUS, "reason": why (empty when ok),
     "figures": [FIGURE, ...]}

    FIGURE
    {"crop_id": "fig_<16 hex>", "file": "crops/<crop_id>.png|.jpg", "sha256", "media_type", "width_px", "height_px",
     "pdf_sha256", "page": 1-based, "page_index": 0-based, "page_size_pt": [w, h],
     "bbox_pt": [x0, y0, x1, y1], "coord": COORD, "dpi",
     "method": one of METHODS, "figure_label": {"value", "status"}, "panel_label": {"value": null, "status": "not_attempted"},
     "caption", "caption_truncated", "caption_source" ("same_page" | "next_page": legend found on the next page
     when the caption only points there), "figure_text" (the crop's own PDF text layer, <= 4000 chars; host checks only, never shown),
     "figure_number" ("3", "S2" or null), "supplementary", "partial" (optional:
     a "(continued)" caption, the figure may be split across pages),
     "selection": {"score", "matched_genes", "matched_terms"},
     "embed": bool, "embed_reason",
     "analysis_status": one of ANALYSIS_STATUS, "analysis_error",
     "analysis": {visible_observations, caption_context, interpretation, relation, relation_rationale,
                  panels_referenced, assay_seen, system_seen, image_quality, confidence} | null,
     "checks": {"removed_caption_copies": int, "unsupported_statistics": [str], "unverified_terms": [str],
                "relation_downgraded_from": relation | null},
     "analysis_key"}

Manual overrides: Programs/P<k>/figure_overrides.json (optional, <= 100 KB), a JSON list of
    {"pmid": "12345678", "page": 1-based int, "bbox_pt": [x0, y0, x1, y1] (optional, COORD space),
     "figure_label": "Figure 3" (optional), "exclude": true (optional: drop that detected figure)}
A bbox entry replaces detection on that page (method manual_override); invalid entries are ignored with a warning.
"""

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote

# HiveBlot principle reuse (eval/blind_2026_10/acquire.py @727b4f0): provenance pins the exact source file by its
# sha256 (pdf_sha256) next to the page and box, so a crop can be traced and re-made. New code.
SCHEMA_VERSION = 1
EXTRACTOR_VERSION = "1"
COORD = (
    "PDF points in unrotated page space (PyMuPDF page.rect * page.derotation_matrix, the space of "
    "get_text/get_drawings), origin top-left, y downward"
)
METHODS = ("caption_anchored_union", "caption_anchored_band", "manual_override")
# HiveBlot principle reuse (western_blot_miner/evidence_record.py:EvidenceField + FigureRef @727b4f0): an uncertain
# field is {value, status} rather than a bare value, and a figure reference keeps page, caption text and crop
# reference together. New code: the status vocabulary below is this step's own.
LABEL_STATUS = ("from_caption", "manual", "uncertain")
ITEM_STATUS = (
    "ok",  # at least one figure analysed
    "pdf_unavailable",  # no open-access PDF for the cited paper
    "extraction_error",  # the PDF could not be read safely (malformed, encrypted, too large, timeout)
    "no_figures",  # the PDF has no captioned figure the extractor accepts
    "no_matching_figure",  # figures exist, none names the cited gene and the claim (no model call made)
    "analysis_failed",  # every selected figure's analysis failed
    "skipped_budget",  # every selected figure was skipped by the spend cap or the call cap
    "pending_session",  # session backend: answers not written yet
    "not_read",  # the cited PMID is not a read paper in literature.jsonl
)
ANALYSIS_STATUS = ("ok", "pending_session", "failed", "skipped_budget")
RELATIONS = ("supports", "contradicts", "not_clearly_addressed", "cannot_assess")
CONTEXT_MATCH = ("same_cell_type", "related", "different", "unknown")

CROP_NAME_RE = re.compile(r"^fig_[0-9a-f]{16}\.(png|jpg)$")
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg"}
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
PMID_RE = re.compile(r"^\d{1,9}$")
PMCID_RE = re.compile(r"^PMC\d{1,9}$")

# Licenses whose crops are embedded in the published viewer by default (redistribution with attribution).
# No-derivatives and unknown licenses: crop kept locally, the viewer shows the link and the analysis only.
EMBED_LICENSES = {
    "cc by",
    "cc by-sa",
    "cc by-nc",
    "cc by-nc-sa",
    "cc0",
    "public domain",
}


def normalize_license(code):
    """'CC BY-NC' / 'cc-by-nc' / 'CC-BY 4.0' -> 'cc by-nc'; '' when unknown."""
    s = str(code or "").strip().lower().replace("_", "-")
    if not s:
        return ""
    s = re.sub(r"\s*\d+(\.\d+)?\s*$", "", s)  # drop a version suffix
    s = re.sub(r"^cc[- ]", "cc ", s)
    if s in ("pd", "public-domain", "publicdomain"):
        return "public domain"
    if s == "cc zero":
        return "cc0"
    return s


def license_allows_embed(code):
    return normalize_license(code) in EMBED_LICENSES


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def crop_id(pdf_sha256, page_index, bbox_pt, dpi):
    """Stable crop id: the same PDF, page, box and resolution always give the same id."""
    key = {
        "pdf_sha256": pdf_sha256,
        "page_index": int(page_index),
        "bbox_pt": [round(float(v), 1) for v in bbox_pt],
        "dpi": int(dpi),
        "extractor_version": EXTRACTOR_VERSION,
    }
    blob = json.dumps(key, sort_keys=True, separators=(",", ":")).encode()
    return "fig_" + hashlib.sha256(blob).hexdigest()[:16]


def paper_links(pmid=None, doi=None, pmcid=None):
    """https links built from validated ids only (never from PDF text or model output)."""
    links = {}
    pmid, doi, pmcid = (
        str(pmid or "").strip(),
        str(doi or "").strip(),
        str(pmcid or "").strip().upper(),
    )
    if PMID_RE.match(pmid):
        links["pubmed"] = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    if PMCID_RE.match(pmcid):
        links["pmc"] = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
    if DOI_RE.match(doi):
        links["doi"] = "https://doi.org/" + quote(doi, safe="/")
    return links


def safe_child(base, name, pattern=None):
    """base/name when name is a plain file name (no separators, optional regex), the file is a regular file and not
    a symlink, and it resolves directly inside base (itself not a symlink); else None."""
    base = Path(base)
    name = str(name or "")
    if not name or "\x00" in name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    if pattern is not None and not re.match(pattern, name):
        return None
    try:
        if base.is_symlink() or not base.is_dir():
            return None
        path = base / name
        if path.is_symlink() or not path.is_file():
            return None
        if path.resolve().parent != base.resolve():
            return None
    except OSError:
        return None
    return path


def figures_dir(run_dir, program):
    """<run_dir>/Synthesis/Figures/<program> (program like 'P3')."""
    return Path(run_dir) / "Synthesis" / "Figures" / program


def validate_record(rec):
    """Structural errors of a figure_evidence.json record (empty list = usable). Used by the viewer, which has no
    pydantic; figure_evidence.py builds records that pass it."""
    errs = []
    if not isinstance(rec, dict):
        return ["record is not an object"]
    if rec.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version {rec.get('schema_version')!r} != {SCHEMA_VERSION}")
    for k in ("program", "label", "interpretation_sha256"):
        if not isinstance(rec.get(k), str) or not rec.get(k):
            errs.append(f"missing {k}")
    items = rec.get("items")
    if not isinstance(items, list):
        return errs + ["items is not a list"]
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            errs.append(f"item {i} is not an object")
            continue
        if it.get("status") not in ITEM_STATUS:
            errs.append(f"item {i}: bad status {it.get('status')!r}")
        if it.get("context_match") not in CONTEXT_MATCH:
            errs.append(f"item {i}: bad context_match {it.get('context_match')!r}")
        for j, f in enumerate(it.get("figures") or []):
            where = f"item {i} figure {j}"
            if not isinstance(f, dict):
                errs.append(f"{where} is not an object")
                continue
            file = str(f.get("file", ""))
            if not re.match(r"^crops/fig_[0-9a-f]{16}\.(png|jpg)$", file):
                errs.append(
                    f"{where}: file must be crops/<crop_id>.png|jpg (no absolute or other paths)"
                )
            if f.get("analysis_status") not in ANALYSIS_STATUS:
                errs.append(f"{where}: bad analysis_status")
            a = f.get("analysis")
            if a is not None and (
                not isinstance(a, dict) or a.get("relation") not in RELATIONS
            ):
                errs.append(f"{where}: bad analysis")
            bbox = f.get("bbox_pt")
            if not (
                isinstance(bbox, list)
                and len(bbox) == 4
                and all(isinstance(v, (int, float)) for v in bbox)
            ):
                errs.append(f"{where}: bad bbox_pt")
    return errs
