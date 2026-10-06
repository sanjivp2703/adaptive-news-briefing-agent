#!/usr/bin/env bash
# Put the fine-tuned briefing model in the product's briefing seat and score it.
#
#   training/serve_student.sh /path/to/news-briefer-q4_k_m.gguf
#
# Installs Ollama if it is missing (via Homebrew on macOS), registers the GGUF
# as the model "news-briefer" with the chat template it was trained on, starts
# the server, scores the student against the teacher on the held-out split, and
# prints the two environment variables that route the product's briefing call
# to it. Everything else in the product keeps using Claude.
set -euo pipefail

GGUF="${1:-}"
MODEL_NAME="${LOCAL_MODEL_NAME:-news-briefer}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -z "$GGUF" || ! -f "$GGUF" ]]; then
  echo "usage: $0 /path/to/news-briefer-q4_k_m.gguf" >&2
  exit 2
fi

if ! command -v ollama >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "Installing Ollama with Homebrew..."
    brew install ollama
  else
    echo "Ollama is not installed and Homebrew is not available. Install it from https://ollama.com and re-run." >&2
    exit 2
  fi
fi

# Start the server if nothing is listening yet.
if ! curl -s -m 2 http://localhost:11434/api/tags >/dev/null; then
  echo "Starting the Ollama server..."
  (ollama serve >/tmp/ollama-serve.log 2>&1 &)
  for _ in $(seq 1 30); do
    curl -s -m 2 http://localhost:11434/api/tags >/dev/null && break
    sleep 1
  done
fi

# Register the model. The template is Qwen's ChatML, no SYSTEM line: the
# product sends the full briefing prompt as the system message on every call.
WORK="$(mktemp -d)"
cp "$GGUF" "$WORK/model.gguf"
cat >"$WORK/Modelfile" <<'EOF'
FROM ./model.gguf
TEMPLATE """{{- range .Messages }}<|im_start|>{{ .Role }}
{{ .Content }}<|im_end|>
{{ end }}<|im_start|>assistant
"""
PARAMETER stop <|im_start|>
PARAMETER stop <|im_end|>
PARAMETER num_ctx 8192
PARAMETER temperature 0.3
EOF
echo "Registering $MODEL_NAME from $GGUF ..."
(cd "$WORK" && ollama create "$MODEL_NAME" -f Modelfile)
rm -rf "$WORK"

export LOCAL_MODEL_BASE_URL="http://localhost:11434/v1"
export LOCAL_MODEL_NAME="$MODEL_NAME"

cd "$ROOT"
PY="${PYTHON:-.venv/bin/python}"

# The untuned base model, scored the same way, is the "before" column.
BASE_MODEL="${BASE_MODEL:-qwen2.5:3b-instruct}"
if ! ollama list | grep -q "^${BASE_MODEL}"; then
  echo "Pulling the untuned base model ($BASE_MODEL) for the before/after comparison..."
  ollama pull "$BASE_MODEL"
fi
echo
echo "Scoring the untuned base model on the held-out split (45 packets)..."
PYTHONPATH=src:. "$PY" training/evaluate_student.py --model "$BASE_MODEL" --out training/data/base_eval.json

echo
echo "Scoring the fine-tuned student on the same packets..."
PYTHONPATH=src:. "$PY" training/evaluate_student.py --out training/data/student_eval.json

echo
echo "To put the student in the product's briefing seat for a live session:"
echo "  export LOCAL_MODEL_BASE_URL=http://localhost:11434/v1"
echo "  export LOCAL_MODEL_NAME=$MODEL_NAME"
echo "  .venv/bin/news-agent-web --open      # the sidebar shows the local model"
