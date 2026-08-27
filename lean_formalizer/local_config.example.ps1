# Copy this file to local_config.ps1 and edit once.
# Loaded automatically by start_translator.ps1 / start_translator.bat

# Folder containing lean-toolchain / lakefile (any Mathlib Lake project root).
# Detected automatically on first-time setup; override if needed.
# $env:LEAN_PROJECT = "C:\path\to\your\mathlib_project"
# $env:LEAN_PROJECT = "/home/you/mathlib_project"

# Optional: lean binary if not on PATH
# $env:LEAN_PATH = "lean"

# Memory directory (defaults to ./memory next to this repo)
# $env:MEMORY_DIR = Join-Path $PSScriptRoot "memory"

# Offline demo (no API calls)
$env:LLM_BACKEND = "mock"

# --- Real LLM (OpenAI-compatible, including CC Switch) ---
# $env:LLM_BACKEND = "openai"
# $env:OPENAI_BASE_URL = "http://127.0.0.1:PORT/v1"
# $env:OPENAI_API_KEY = "sk-..."
# $env:OPENAI_MODEL = "deepseek-chat"

# --- Anthropic ---
# $env:LLM_BACKEND = "anthropic"
# $env:ANTHROPIC_API_KEY = "sk-ant-..."
# $env:ANTHROPIC_MODEL = "claude-sonnet-4-20250514"

# $env:LLM_TIMEOUT = "300"
# $env:JOB_TIMEOUT = "1200"
