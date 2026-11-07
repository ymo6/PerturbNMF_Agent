"""Figure evidence in program_viewer.py: what is embedded, what is refused, escaping, size caps, publishing, and that a
viewer without figures carries the same PROGRAMS data as before the figure card existed."""

import hashlib
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import program_viewer
import publish_viewer
import pytest

ROOT = Path(__file__).resolve().parents[4]
SYN = ROOT / "src" / "Stage3_Interpretation" / "C_Annotation" / "2.Synthesis"
LABEL = "Ribosome biogenesis"
URI_RE = re.compile(r"^data:image/(png|jpeg);base64,[A-Za-z0-9+/=]+$")
NODE = shutil.which("node")


def png(w=1, h=1, seed=0):
    """A real PNG of random RGB pixels (stored, so ~3*w*h bytes)."""
    rnd = random.Random(seed)
    raw = b"".join(b"\x00" + rnd.randbytes(3 * w) for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 0))
        + chunk(b"IEND", b"")
    )


def make_run(tmp, n=1):
    run = tmp / "run"
    for k in range(1, n + 1):
        d = run / "Programs" / f"P{k}"
        d.mkdir(parents=True)
        (d / "label.json").write_text(
            json.dumps(
                {
                    "program_name": LABEL,
                    "function_sentence": "Makes ribosomes.",
                    "model": "m",
                }
            )
        )
        (d / "program.json").write_text(json.dumps({"conditions": ["D0"]}))
        slot = {"claim": "MYC drives it.", "confidence": "medium", "pmids": ["12345"]}
        (d / "interpretation.json").write_text(
            json.dumps(
                {
                    "label": LABEL,
                    "confounders": [],
                    "interpretation": {
                        "upstream_trigger": slot,
                        "coregulation_mechanism": None,
                        "cellular_output": None,
                    },
                    "modules": [],
                    "coherence": "high",
                    "model": "m",
                }
            )
        )
    return run


def add_figures(
    run,
    k=1,
    crops=None,
    caption="Figure 3. MYC knockdown lowers rRNA.",
    embed=True,
    base=None,
    license="CC BY",
):
    """Write crops + a valid record for P<k>; returns the record (rewrite it with save())."""
    crops = [png()] if crops is None else crops
    pdir = (base or run / "Synthesis" / "Figures") / f"P{k}"
    (pdir / "crops").mkdir(parents=True, exist_ok=True)
    figs = []
    for i, data in enumerate(crops):
        cid = "fig_" + hashlib.sha256(data + bytes([k, i])).hexdigest()[:16]
        (pdir / "crops" / f"{cid}.png").write_bytes(data)
        figs.append(
            {
                "crop_id": cid,
                "file": f"crops/{cid}.png",
                "sha256": hashlib.sha256(data).hexdigest(),
                "media_type": "image/png",
                "width_px": 1,
                "height_px": 1,
                "page": 4,
                "bbox_pt": [10.0, 20.0, 300.0, 400.0],
                "coord": "PDF points, origin top-left",
                "dpi": 150,
                "method": "caption_anchored_union",
                "figure_label": {"value": "Figure 3", "status": "from_caption"},
                "panel_label": {"value": None, "status": "not_attempted"},
                "caption": caption,
                "caption_truncated": False,
                "selection": {
                    "score": 2,
                    "matched_genes": ["MYC"],
                    "matched_terms": [],
                },
                "embed": embed,
                "embed_reason": "",
                "analysis_status": "ok",
                "analysis_error": "",
                "analysis": {
                    "visible_observations": ["Bars lower after knockdown."],
                    "caption_context": ["rRNA measured by qPCR."],
                    "interpretation": "Lower rRNA after MYC loss.",
                    "relation": "supports",
                    "relation_rationale": "",
                    "confidence": "medium",
                },
                "checks": {
                    "unsupported_statistics": ["p < 0.001"],
                    "unverified_terms": [],
                    "removed_caption_copies": 0,
                },
            }
        )
    rec = {
        "schema_version": 1,
        "program": f"P{k}",
        "label": LABEL,
        "interpretation_sha256": hashlib.sha256(
            (run / "Programs" / f"P{k}" / "interpretation.json").read_bytes()
        ).hexdigest(),
        "screen_context": "lymphoblast (human)",
        "model": "test-model",
        "items": [
            {
                "item_id": "upstream_trigger:12345",
                "slot": "upstream_trigger",
                "slot_claim": "MYC drives it.",
                "pmid": "12345",
                "paper": {
                    "pmid": "12345",
                    "doi": "10.1000/xyz",
                    "pmcid": "PMC1",
                    "title": "MYC and ribosomes",
                    "year": 2020,
                    "license": license,
                },
                "sub_claim": "MYC knockdown lowers rRNA.",
                "paper_context": "fibroblasts",
                "context_match": "different",
                "status": "ok",
                "reason": "",
                "figures": figs,
            }
        ],
    }
    save(pdir, rec)
    return rec


def save(pdir, rec):
    (pdir / "figure_evidence.json").write_text(json.dumps(rec))


def embedded(html, name):
    """(object, raw text) of `const <name> = {...}` as publish_viewer.viewer_programs reads it."""
    m = re.search(rf"const {name}\s*=\s*", html)
    obj, end = json.JSONDecoder().raw_decode(html[m.end() :])
    return obj, html[m.end() : m.end() + end]


def run_viewer(run, capsys, monkeypatch, *extra):
    out = run / "Report" / "program_viewer.html"
    argv = [
        "program_viewer.py",
        "--run_dir",
        str(run),
        "--out",
        str(out),
        "--dataset_name",
        "Test",
        *extra,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    assert program_viewer.main() == 0
    return out.read_text(), capsys.readouterr().out


def run_original(run, tmp):
    """The committed program_viewer.py + template (before the figure card) on the same run."""
    old = tmp / "old"
    old.mkdir()
    for f in ("program_viewer.py", "program_viewer_template.html"):
        rel = (SYN / f).relative_to(ROOT).as_posix()
        src = subprocess.run(
            ["git", "show", f"HEAD:{rel}"],
            check=False,
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if src.returncode:
            pytest.skip("no committed program_viewer.py to compare with")
        (old / f).write_text(src.stdout)
    out = tmp / "old.html"
    env = {**os.environ, "PYTHONPATH": str(SYN)}
    cmd = [
        sys.executable,
        str(old / "program_viewer.py"),
        "--run_dir",
        str(run),
        "--out",
        str(out),
        "--dataset_name",
        "Test",
    ]
    subprocess.run(cmd, check=True, env=env, capture_output=True)
    return out.read_text()


def first_fig(html):
    return embedded(html, "PROGRAMS")[0]["1"]["figures"]["items"][0]["figures"][0]


# ---- backwards compatibility ----------------------------------------------------------------------------------------
def test_no_figures_programs_json_identical_to_original(tmp_path, capsys, monkeypatch):
    run = make_run(tmp_path)
    old = embedded(run_original(run, tmp_path), "PROGRAMS")[1]
    new_html, _ = run_viewer(run, capsys, monkeypatch)
    assert embedded(new_html, "PROGRAMS")[1] == old
    assert embedded(new_html, "FIGURES")[0] == {}
    add_figures(run)
    flag_html, _ = run_viewer(run, capsys, monkeypatch, "--no_figures")
    assert embedded(flag_html, "PROGRAMS")[1] == old
    assert embedded(flag_html, "FIGURES")[0] == {}
    assert not (run / "Report" / "program_viewer.figures.json").exists()


# ---- shown ----------------------------------------------------------------------------------------------------------
def test_figures_shown(tmp_path, capsys, monkeypatch):
    run = make_run(tmp_path)
    rec = add_figures(run)
    html, out = run_viewer(run, capsys, monkeypatch)
    cid = rec["items"][0]["figures"][0]["crop_id"]
    figs = embedded(html, "FIGURES")[0]
    assert URI_RE.match(figs[cid])
    f = first_fig(html)
    assert f["has_image"] and f["image_note"] == "" and f["crop_id"] == cid
    assert "file" not in f and "sha256" not in f
    meta = embedded(html, "PROGRAMS")[0]["1"]["figures"]
    assert str(tmp_path) not in json.dumps(meta)
    assert (
        meta["items"][0]["paper"]["links"]["pubmed"]
        == "https://pubmed.ncbi.nlm.nih.gov/12345/"
    )
    assert "P1: 1 figure(s) shown, 1 embedded" in out
    side = json.loads((run / "Report" / "program_viewer.figures.json").read_text())
    assert side["programs"]["P1"] == meta and "data:" not in json.dumps(side)


def _expect_hidden(run, capsys, monkeypatch, warning):
    html, out = run_viewer(run, capsys, monkeypatch)
    assert "figures" not in embedded(html, "PROGRAMS")[0]["1"]
    assert warning in out


def test_label_mismatch_hidden(tmp_path, capsys, monkeypatch):
    run = make_run(tmp_path)
    rec = add_figures(run)
    rec["label"] = "Old label"
    save(run / "Synthesis" / "Figures" / "P1", rec)
    _expect_hidden(run, capsys, monkeypatch, 'figures were made for "Old label"')


def test_interpretation_changed_hidden(tmp_path, capsys, monkeypatch):
    run = make_run(tmp_path)
    add_figures(run)
    p = run / "Programs" / "P1" / "interpretation.json"
    p.write_text(p.read_text() + " ")
    _expect_hidden(
        run, capsys, monkeypatch, "older interpretation; rerun figure_evidence.py"
    )


def test_bad_crop_name_invalid(tmp_path, capsys, monkeypatch):
    run = make_run(tmp_path)
    rec = add_figures(run)
    rec["items"][0]["figures"][0]["file"] = "crops/../../Programs/P1/x.png"
    save(run / "Synthesis" / "Figures" / "P1", rec)
    _expect_hidden(run, capsys, monkeypatch, "invalid")


def test_forged_record_in_programs_ignored(tmp_path, capsys, monkeypatch):
    run = make_run(tmp_path)
    add_figures(
        run, base=run / "Programs"
    )  # Programs/P1/figure_evidence.json + crops: agent-writable, never read
    html, _ = run_viewer(run, capsys, monkeypatch)
    assert "figures" not in embedded(html, "PROGRAMS")[0]["1"]
    assert embedded(html, "FIGURES")[0] == {}


def test_crop_sha_mismatch_no_image(tmp_path, capsys, monkeypatch):
    run = make_run(tmp_path)
    rec = add_figures(run)
    cid = rec["items"][0]["figures"][0]["crop_id"]
    (run / "Synthesis" / "Figures" / "P1" / "crops" / f"{cid}.png").write_bytes(
        png(seed=9)
    )
    html, _ = run_viewer(run, capsys, monkeypatch)
    f = first_fig(html)
    assert not f["has_image"] and f["image_note"] == "crop missing or changed"
    assert embedded(html, "FIGURES")[0] == {}


def test_symlinked_crop_no_image(tmp_path, capsys, monkeypatch):
    run = make_run(tmp_path)
    rec = add_figures(run)
    cid = rec["items"][0]["figures"][0]["crop_id"]
    crop = run / "Synthesis" / "Figures" / "P1" / "crops" / f"{cid}.png"
    real = tmp_path / "elsewhere.png"
    real.write_bytes(crop.read_bytes())
    crop.unlink()
    crop.symlink_to(real)
    html, _ = run_viewer(run, capsys, monkeypatch)
    assert not first_fig(html)["has_image"]
    assert embedded(html, "FIGURES")[0] == {}


def test_embed_false_listed_without_image(tmp_path, capsys, monkeypatch):
    run = make_run(tmp_path)
    add_figures(run, embed=False)
    html, _ = run_viewer(run, capsys, monkeypatch)
    f = first_fig(html)
    assert not f["has_image"] and f["image_note"] == "not embedded: license CC BY"
    assert embedded(html, "FIGURES")[0] == {} and "data:image/png;base64," not in html


def test_embed_flag_decides_not_license(tmp_path, capsys, monkeypatch):
    """figure_evidence.py sets embed (license rule or --embed_license all); the viewer follows it."""
    run = make_run(tmp_path)
    add_figures(run, embed=True, license="CC BY-ND")
    html, _ = run_viewer(run, capsys, monkeypatch)
    assert first_fig(html)["has_image"]
    assert URI_RE.match(next(iter(embedded(html, "FIGURES")[0].values())))


# ---- escaping -------------------------------------------------------------------------------------------------------
NASTY = 'x</script><script>alert(1)</script> <!--   __META_JSON__ __FIGURES_JSON__ __PROGRAMS_JSON__ "q"'


def test_caption_escaping(tmp_path, capsys, monkeypatch):
    run = make_run(tmp_path)
    add_figures(run, caption=NASTY)
    html, _ = run_viewer(run, capsys, monkeypatch)
    template = (SYN / "program_viewer_template.html").read_text()
    assert first_fig(html)["caption"] == NASTY
    assert html.count("</script") == template.count("</script")
    rest = html
    for name in (
        "PROGRAMS",
        "META",
        "FIGURES",
    ):  # outside the data, the page has the template's script tags only
        rest = rest.replace(embedded(html, name)[1], "")
    assert rest.count("<script") == template.count("<script")
    assert "<!--" not in html.split("<script>", 1)[1]
    assert " " not in html
    assert URI_RE.match(next(iter(embedded(html, "FIGURES")[0].values())))


# ---- the page's JavaScript (node) -----------------------------------------------------------------------------------
STUBS = """
const store = {};
const mk = id => ({id, textContent: "", style: {}, dataset: {}, value: "", classList: {toggle() {}},
  set innerHTML(v) { store[id] = v; }, get innerHTML() { return store[id] || ""; }});
const els = {};
globalThis.window = globalThis;
globalThis.document = {getElementById: id => els[id] ||= mk(id), querySelectorAll: () => [], querySelector: () => null,
  addEventListener() {}, documentElement: {dataset: {}}};
globalThis.scrollTo = () => {}; globalThis.addEventListener = () => {};
for (const [k, v] of Object.entries({history: {replaceState() {}}, location: {hash: ""}, matchMedia: () => ({matches: false}),
  localStorage: {getItem: () => null, setItem() {}}, getComputedStyle: () => ({getPropertyValue: () => "#000000"})}))
  Object.defineProperty(globalThis, k, {value: v, configurable: true, writable: true});
"""


def run_page(html, tmp_path, tail):
    """Run the viewer's script in node with a stub DOM, then `tail`; returns its JSON output."""
    if not NODE:
        pytest.skip("node not available")
    script = html.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    js = tmp_path / "page.js"
    js.write_text(
        STUBS + script + "\n;process.stdout.write(JSON.stringify(" + tail + "));"
    )
    return json.loads(
        subprocess.run(
            [NODE, str(js)], check=True, capture_output=True, text=True
        ).stdout
    )


def test_page_renders_figure_card(tmp_path, capsys, monkeypatch):
    run = make_run(tmp_path)
    add_figures(run, caption=NASTY)
    html, _ = run_viewer(run, capsys, monkeypatch)
    main = run_page(html, tmp_path, 'document.getElementById("main").innerHTML')
    assert "Figure evidence from cited papers" in main
    assert re.search(
        r'<img src="data:image/png;base64,[A-Za-z0-9+/=]+" alt="Figure 3 from MYC and ribosomes"',
        main,
    )
    assert "Figure consistent with the cited finding (different system)" in main
    assert (
        "Paper system: fibroblasts · This screen: lymphoblast (human) · different"
        in main
    )
    assert "Numbers not visible in the figure or caption: p &lt; 0.001" in main
    assert (
        "<script>alert" not in main and "&lt;/script&gt;&lt;script&gt;alert(1)" in main
    )
    assert not re.search(r"validated|confirmed|proves", main, re.IGNORECASE)


def test_safe_url(tmp_path, capsys, monkeypatch):
    run = make_run(tmp_path)
    html, _ = run_viewer(run, capsys, monkeypatch)
    got = run_page(
        html,
        tmp_path,
        '[safeUrl("javascript:alert(1)"), safeUrl(" https://a.b"), safeUrl("HTTPS://a.b/x"), safeUrl("data:text/html,x"),'
        ' safeUrl(null), paperItem({title: "T", url: "javascript:alert(1)"}), paperItem({title: "T", url: "https://a.b/"}),'
        ' safeUrl("http://a.b/y"), safeUrl("//a.b"), safeUrl("JavaScript:http://x")]',
    )
    assert got[:5] == ["", "", "HTTPS://a.b/x", "", ""]
    assert "<a" not in got[5] and 'href="https://a.b/"' in got[6]
    assert got[7:] == ["http://a.b/y", "", ""]


def test_legend_from_next_page_note(tmp_path, capsys, monkeypatch):
    run = make_run(tmp_path)
    rec = add_figures(run)
    rec["items"][0]["figures"][0]["caption_source"] = "next_page"
    save(run / "Synthesis" / "Figures" / "P1", rec)
    html, _ = run_viewer(run, capsys, monkeypatch)
    assert first_fig(html)["caption_source"] == "next_page"
    main = run_page(html, tmp_path, 'document.getElementById("main").innerHTML')
    assert "(legend from the next page)" in main


def test_page_without_figures_matches_original(tmp_path, capsys, monkeypatch):
    """No figures: no figure card, and the rendered program page equals the committed template's."""
    run = make_run(tmp_path)
    old_html = run_original(run, tmp_path)
    new_html, _ = run_viewer(run, capsys, monkeypatch)
    tail = 'document.getElementById("main").innerHTML'
    old_main = run_page(old_html, tmp_path, tail)
    new_main = run_page(new_html, tmp_path, tail)
    assert "Figure evidence" not in new_main and "Layered interpretation" in new_main
    assert new_main == old_main


def test_csp_present(tmp_path, capsys, monkeypatch):
    html, _ = run_viewer(make_run(tmp_path), capsys, monkeypatch)
    assert "default-src 'none'; img-src data:;" in html


# ---- size caps ------------------------------------------------------------------------------------------------------
def test_size_cap(tmp_path, capsys, monkeypatch):
    run = make_run(tmp_path, n=10)
    for k in range(1, 11):
        add_figures(run, k, crops=[png(300, 333, seed=k * 10 + i) for i in range(3)])
    html, out = run_viewer(run, capsys, monkeypatch, "--max_figure_mb", "1")
    figs = embedded(html, "FIGURES")[0]
    assert 0 < sum(map(len, figs.values())) <= 1e6
    assert "over --max_figure_mb 1" in out
    progs = embedded(html, "PROGRAMS")[0]
    all_figs = [
        f for p in progs.values() for it in p["figures"]["items"] for f in it["figures"]
    ]
    dropped = [f for f in all_figs if not f["has_image"]]
    assert len(dropped) == 30 - len(figs) and all(
        f["image_note"] == "dropped: size cap" for f in dropped
    )
    assert {f["crop_id"] for f in all_figs if f["has_image"]} == set(figs)


def test_per_program_cap(tmp_path, capsys, monkeypatch):
    run = make_run(tmp_path)
    add_figures(run, crops=[png(seed=i) for i in range(3)])
    html, out = run_viewer(
        run, capsys, monkeypatch, "--max_figures_embedded_per_program", "2"
    )
    assert len(embedded(html, "FIGURES")[0]) == 2
    assert "P1: 3 figure(s) shown, 2 embedded" in out


# ---- publishing -----------------------------------------------------------------------------------------------------
def test_publish_viewer_with_figures(tmp_path, capsys, monkeypatch):
    run = make_run(tmp_path)
    add_figures(run)
    run_viewer(run, capsys, monkeypatch)
    viewer = run / "Report" / "program_viewer.html"
    assert publish_viewer.viewer_programs(viewer) == ["P1"]
    site = tmp_path / "site"
    argv = [
        "publish_viewer.py",
        "--viewer",
        str(viewer),
        "--site_dir",
        str(site),
        "--dataset_name",
        "Test set",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    assert publish_viewer.main() == 0
    pub = site / "test_set"
    assert (pub / "viewer.figures.json").read_text() == viewer.with_suffix(
        ".figures.json"
    ).read_text()
    assert embedded((pub / "viewer.html").read_text(), "FIGURES")[0]
