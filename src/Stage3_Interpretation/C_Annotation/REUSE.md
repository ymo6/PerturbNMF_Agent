# Reuse manifest: figure evidence

The figure-evidence code (`2.Synthesis/figure_*.py`, image input in `api/`) reuses design principles from HiveBlot
(a western-blot evidence project, MIT license; source revision `727b4f0`). No HiveBlot code was copied: every block
below is new code written from the principle, and each carries a `# HiveBlot principle reuse (...)` comment next to
it. Paths are HiveBlot paths.

| HiveBlot source (path : symbol) | Reuse | Principle adopted | Where here |
|---|---|---|---|
| `western_blot_miner/llm_client.py` : `AnthropicClient.complete_json` | principle | image as a base64 content block before the text; a refusal is an error | `api/LLM_interface.py` `claude_api.get_json` |
| `western_blot_miner/pdf_preprocess.py` : `load_for_vlm` | principle | cap an image's long side before a vision call (here via the render dpi, no Pillow) | `2.Synthesis/figure_extract.py` render |
| `api/app/experimental/uploads.py` : `_INSPECT` | principle | parse untrusted PDFs in a child process (`-I`, minimal env, CPU / memory limits, page cap, refuse encrypted or page-less files) | `2.Synthesis/figure_extract.py` worker |
| `western_blot_miner/evidence_record.py` : `EvidenceField`, `FigureRef` | principle | uncertain fields as `{value, status}`; a figure reference keeps page, caption and crop together | `2.Synthesis/figure_contract.py` `LABEL_STATUS` and the FIGURE record |
| `western_blot_miner/vlm_extract.py` : module prompt | principle | the image is the source of truth for what is visible; paper text only gives caption context | `prompts/figure_analysis/`, loaded in `figure_evidence.py` |
| `western_blot_miner/vlm_extract.py` : `_should_retry_extraction` | principle | retry only an answer that failed to parse | `2.Synthesis/figure_evidence.py` `analyse` |
| `eval/blind_2026_10/acquire.py` | principle | pin the source file by sha256 in provenance | `2.Synthesis/figure_contract.py` (`pdf_sha256`) |

Considered and not reused: HiveBlot's PDF fetcher (PerturbNMF already downloads open-access PDFs in
`agent/paper-fetch-skill`), its OpenCV crop scoring (tuned for blots), its web rendering, and its third-party figure
crops and caption excerpts (publisher content, never copied).
