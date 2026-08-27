# High-Accuracy NL / PDF → Lean 4 Formalization Pipeline

Production-oriented end-to-end workflow that turns mathematical text (PDF or plain language) into **verified Lean 4** code.

```
PDF / Natural Language Text
        ↓
1. Text Extraction & Cleaning
        ↓
2. Structure & Decomposition
        ↓
3. Memory Retrieval (Positive + Negative examples)
        ↓
4. Multi-Stage Prompting
        ↓
5. Generate Multiple Candidates
        ↓
6. Lean Verification + Automatic Repair Loop
        ↓
7. Ranking & Selection
        ↓
8. Save to Memory (Success / Failure+Fix)
        ↓
Final Verified Lean 4 Code
```

## Features

- **PDF extraction** via `pdfplumber` / `pypdf` (digital text preferred)
- **Structure decomposition** (definitions, theorem, lemmas, proof sketch)
- **Memory bank** with TF-IDF retrieval of successful formalizations and repair examples
- **Multi-stage LLM prompting** (understand → statement → proof → self-critique)
- **Multi-candidate generation** + ranking
- **Lean 4 verification + automatic repair loop** (uses local `lean` if available)
- **Pluggable LLM backends** (mock for offline demo, OpenAI / Anthropic / HTTP when configured)
- Fully runnable offline with the included mock LLM

## Quick Start — Web UI (opens in browser)

```bash
cd lean_formalizer
export PYTHONPATH=src
python -m lean_formalizer.webapp
# → http://127.0.0.1:8765  (opens automatically)
```

The website exposes every pipeline control:

- Text input **or** PDF upload
- Domain selection
- LLM backend (mock / OpenAI / Anthropic / HTTP) + API keys
- Candidates, repair rounds, temperature
- Save-to-memory toggle
- Live result: structure, best Lean code, all candidates, memory hits
- Doctor (environment check) & Memory bank stats

## Quick Start — CLI

```bash
cd lean_formalizer
export PYTHONPATH=src
python -m lean_formalizer.cli formalize --text "Every natural number is either even or odd." --domain number_theory
```

Or from a PDF:

```bash
python -m lean_formalizer.cli formalize --pdf path/to/paper.pdf --domain algebra
```

## Project Layout

```
lean_formalizer/
├── src/lean_formalizer/
│   ├── __init__.py
│   ├── cli.py                 # Command-line entry point
│   ├── webapp.py              # Web UI server (stdlib HTTP, opens in browser)
│   ├── static/                # index.html + CSS + JS
│   ├── pipeline.py            # Orchestrator of the 8 steps
│   ├── extract.py             # PDF / text cleaning
│   ├── structure.py           # Decomposition into defs / thm / lemmas / sketch
│   ├── memory.py              # JSON + TF-IDF memory store
│   ├── prompts.py             # Multi-stage prompt templates
│   ├── llm.py                 # Pluggable LLM clients (Mock / OpenAI / Anthropic / HTTP)
│   ├── lean_verify.py         # Compile + repair loop
│   ├── rank.py                # Candidate ranking
│   └── models.py              # Dataclasses
├── memory/                    # Persistent memory bank (JSON)
├── examples/                  # Sample inputs
├── scripts/
│   └── seed_memory.py
├── requirements.txt
└── README.md
```

## Configuration

Environment variables (or pass via CLI):

| Variable            | Meaning                                      | Default        |
|---------------------|----------------------------------------------|----------------|
| `LLM_BACKEND`       | `mock` / `openai` / `anthropic` / `http`     | `mock`         |
| `OPENAI_API_KEY`    | OpenAI key                                   | —              |
| `ANTHROPIC_API_KEY` | Anthropic key                                | —              |
| `LLM_HTTP_URL`      | Custom chat-completions endpoint             | —              |
| `LEAN_PATH`         | Path to `lean` binary                        | `lean`         |
| `MEMORY_DIR`        | Directory for memory JSON files              | `./memory`     |
| `NUM_CANDIDATES`    | How many candidates to generate              | `4`            |
| `MAX_REPAIR_ROUNDS` | Max automatic repair attempts per candidate  | `3`            |
| `TEMPERATURE`       | Sampling temperature                         | `0.2`          |

## Memory Growth

| # examples | Expected behavior                                      |
|------------|--------------------------------------------------------|
| 0–20       | Decent baseline                                        |
| 50–100     | More consistent style, fewer basic errors              |
| 200+       | Strong domain adaptation, better complex formalization |

Never store unverified code. Periodically prune low-quality entries.

## Production Notes

1. Always formalize **statement first**, then proof.
2. Prefer tactic-mode proofs (easier to repair).
3. Keep temperature low (0.1–0.3).
4. Use a strong closed-source model (Claude Opus / GPT-4o / Gemini) for best results.
5. For real embeddings replace the TF-IDF store with Chroma / FAISS + `sentence-transformers` or OpenAI embeddings.
6. Run Lean with Mathlib via a Lake project or LeanDojo for industrial use.

### Lean project

Set `LEAN_PROJECT` to any Lake+Mathlib project root (or let first-time autoconfig detect one).  
Verification writes temporary files under `<LEAN_PROJECT>/FormalizerScratch/run_*/Candidate.lean` and type-checks them with `lake env lean`.  

Add `FormalizerScratch/` to that project’s `.gitignore` to keep scratch files out of version control.

## License

MIT
