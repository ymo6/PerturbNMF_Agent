#!/bin/bash
#SBATCH --job-name=figure_evidence
#SBATCH -p <partition>
#SBATCH --time=02:00:00
#SBATCH --cpus-per-task=2
#SBATCH --mem=8GB
#SBATCH --output=logs/figure_evidence.%j.log

# Usage: export PIPELINE_ROOT=/path/to/PerturbNMF; sbatch run_figure_evidence.sh
# Edit RESULT and the args below; every arg is listed, defaults included.
# After the interpretation step (run_program_agent.sh): for each interpretation slot and each paper it cites, crops
# the captioned figures of the paper's open-access PDF (Programs/P<k>/papers), keeps those whose caption names a
# cited gene and the claim, and asks Claude what each figure shows against the paper's recorded finding. Writes
# <RUN_DIR>/Synthesis/Figures/P<k>/figure_evidence.json + crops/; run_program_viewer.sh then shows them.
# --llm_backend session (default) writes the requests to <RUN_DIR>/Session_LLM/figure_evidence for a Claude Code
# session to answer (open each listed image), then run this again to import them; --llm_backend api bills
# ANTHROPIC_API_KEY (C_Annotation/.env), capped by --budget_usd per program. Needs the geneqa environment
# (pymupdf, pydantic; anthropic for the api backend). Untrusted PDFs are parsed in a time-limited child process.

set -euo pipefail
: "${PIPELINE_ROOT:?set PIPELINE_ROOT to the PerturbNMF repo root}"
eval "$(conda shell.bash hook)"
conda activate geneqa

SCRIPT_DIR=$PIPELINE_ROOT/src/Stage3_Interpretation/C_Annotation/2.Synthesis
RESULT=/path/to/out_dir/run_name
RUN_DIR=$RESULT/Interpretation/Annotation
PROGRAMS=""   # e.g. "--programs 1 2 3"; empty = every program with an interpretation of its current label

python $SCRIPT_DIR/figure_evidence.py \
  --run_dir $RUN_DIR \
  $PROGRAMS \
  --out_dir $RUN_DIR/Synthesis/Figures \
  --llm_backend session \
  --session_dir $RUN_DIR/Session_LLM \
  --session_model claude-code-session \
  --model claude-sonnet-5 \
  --effort low \
  --max_tokens 2000 \
  --max_figures_per_slot 2 \
  --max_calls_per_program 6 \
  --embed_license default \
  --pdf_timeout 90 \
  --budget_usd 2.0 \
  --budget_reserve_usd 0.0
# add --fetch_missing to download missing open-access PDFs, --no_license_lookup to skip the license lookup
# (crops of unknown license are then not embedded), --overwrite to redo cached analyses
