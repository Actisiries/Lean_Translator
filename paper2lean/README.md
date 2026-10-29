# Paper2Lean

`paper2lean` is a paper-oriented orchestration layer for translating PDF/TeX
mathematics into verified Lean 4 modules. It keeps extraction and verification
deterministic, uses an OpenAI-compatible HTTP LLM only for interpretation and
candidate generation, and records every intermediate artifact.

The package is intentionally independent of the legacy web application. It can
be used directly from Python and does not start or import the legacy web server.

## Independent web application

Start it from PowerShell:

```powershell
cd C:\Users\Anson Kam\Lean_Work
& .tools\python-runtime\python.exe paper2lean\run.py web
```

Or double-click `start_paper2lean.bat`. The independent UI opens at
`http://127.0.0.1:8787`. It accepts PDF and TeX uploads and lets the user enter
the HTTP endpoint, model, API key, and candidate count in the page.

The workbench uses a two-pane layout: source and worker controls are on the
left, while inventory and formalization results are on the right. History and
Statistics are separate tabs, and the sun button switches day/night mode.

The independent API is:

```text
GET  /api/health
POST /api/upload
POST /api/analyze
POST /api/run
GET  /api/paper2lean/status?paper_id=<id>
```

The legacy `lean_formalizer` server is not required for this application.

## Run controls and progress

Analysis and formalization run in the original HTTP request. The workbench
shows analyzed units as individual cards, including each label and statement.
Full extracted source remains in the saved run artifacts.

The latest five steps are observed through `GET /api/activity?request_id=<id>`.
This endpoint only reads active request state: it cannot start, resume, or
restart work. Monitoring failures do not fail the processing request. A server
restart clears active request state; an unknown status is shown as unconfirmed.
Keep the server and browser open until the direct request returns.

The default time budget is **600 seconds per unit**, shared by representation
planning, Mathlib checks, all candidates, repairs, and audits. Change the minutes
in the workbench, set `PAPER2LEAN_UNIT_BUDGET_SECONDS`, or pass
`--unit-budget-seconds` to the CLI. HTTP timeouts and retries and Lean compiler
timeouts are capped by the remaining unit budget. A unit that exhausts its
budget is saved as `timed_out`, and processing moves on. Extraction, analysis,
and the final assembly have their own existing timeouts, outside this allowance.
Injected custom Python LLM/compiler callbacks must implement their own blocking
I/O timeout; the budget is checked before and after they return.

Repairs stop when code is empty/unchanged or when the same compiler failure
reappears (ignoring scratch filenames and source positions). Independent
candidates may still try a different strategy within the shared unit budget.

Each repair prompt includes the current compiler diagnostic and all earlier
code/diagnostic pairs for that candidate. Returning any earlier candidate code
stops the loop, including cycles back to an older attempt. These pairs are
saved after each check in `units/<id>/attempts/candidate_<n>_history.json`.

After formalization, the workbench shows compiled, failed/timed-out, and pending
unit counts, together with the assembly result. **Retry all failed units…**
lists the failed units in a confirmation dialog. Confirming starts one new pass
over failed, timed-out, or blocked units; successful and pending units are kept.
There is no automatic retry pass. Previous failed code and compiler reports
are passed to the LLM, and previous unit records are archived under
`units/<id>/history/`. Each retried unit receives the configured time budget.
The API requires both `retry_failed: true` and `retry_confirmed: true`.
These outcomes measure compilation, not proof fidelity to the source paper.

All page inputs and buttons are disabled while analysis or formalization is
pending, including endpoint settings, worker counts, navigation, and retry
controls. They are restored after the request and final status check finish.

Representation planning proposes exact `mathlib_names`. Lean checks those names
with `#check` using the proposed imports before declaration/proof generation;
missing-name diagnostics are supplied to later prompts. An unavailable compiler
is reported separately. This checks proposed names, not every name an LLM may
subsequently invent; final Lean compilation remains required. Valid search
tactics such as `exact?` are not classified as placeholder holes.

## Local Git workflow

This application has its own local repository. No remote service is required.
Run these commands inside the `paper2lean` folder:

```powershell
git status                         # See changed files
git diff                           # Review edits before saving
git switch -c feature/my-change     # Create a branch for a change
git add src static tests README.md  # Select reviewed files
git commit -m "Describe the change" # Save a snapshot
git log --oneline                   # Browse saved versions
```

Create small commits after a meaningful, tested change. To undo a committed
change while preserving history, use `git revert <commit-id>`. Use
`git show <commit-id>` to inspect a snapshot. The pre-change checkpoint is tagged
`checkpoint-before-runtime-improvements-20261018`.

Git tracks code history; the local repository is not an off-device backup.
Optionally add a remote later and push branches for backup or collaboration.
`local_config.env`, API secrets, Python caches, and run artifacts are ignored.

## Checks

Extraction reads standard TeX theorem declarations, including aliases, shared
counters, section/subsection resets, starred environments and optional titles.
Printed names (for example, `Proposition 3.1`) are stored separately from
reference labels (for example, `prop:3.4`). Statements retain their source
mathematics; normalization does not ask a model to rewrite them. Comments,
verbatim examples and command definitions do not create claims.

Each local proof has one source owner. Explicit proof titles can link delayed
proofs; unnamed proofs attach only to an adjacent claim without an intervening
claim, section, or substantial prose. Model suggestions cannot steal or erase
source-owned proofs. Uncertain suggestions are retained for review without
attaching their text as a verified proof. The workbench displays printed names,
reference labels, source proof text, ownership evidence and missing/cited/ambiguous
statuses.

PDF extraction retains page boundaries and page-local source spans, including
proofs continued on another page. Pages without extractable text are flagged
as incomplete and require OCR. PDF reading order and mathematical symbols still
need source review. TeX extraction is a structural reader, not a TeX engine:
included files are flagged rather than expanded, and custom numbering macros
are reported rather than guessed. Supply a combined TeX source for such projects.
Reanalyze existing inputs to populate the new extraction fields. These changes
do not alter formalization, compiler checks, retry behavior, or run folders.

With the workspace's bundled Python runtime, run the regression suite from
the `Lean_Work` folder:

```powershell
& .tools\python-runtime\python.exe -c "import sys,unittest; sys.path[:0]=['paper2lean/src','paper2lean/tests']; import test_runtime; result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(test_runtime)); sys.exit(not result.wasSuccessful())"
node paper2lean/tests/test_ui.cjs
```

Run the extraction regressions (including the saved LX-CPP source) with:

```powershell
& .tools\python-runtime\python.exe -m unittest discover -s paper2lean/tests -p test_extraction.py -v
```
