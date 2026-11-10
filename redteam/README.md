# Application red-team suite

This suite runs the underwriting application through **PyRIT 1.1.0** and
**garak 0.17.1**, pinned independently of the backend. It targets the application
API, including resolution, approval, report generation, validation and cleanup.
It does not expose a new production endpoint or bypass approval.

## Install and run offline

Use Python 3.12 and uv 0.13.7, with the backend environment installed first.
`redteam/pyproject.toml` and `redteam/uv.lock` are independent of the backend lock.
From the project root in PowerShell:

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$PWD/.venv-redteam"
uv sync --project redteam --locked --python .venv/Scripts/python.exe
Remove-Item Env:UV_PROJECT_ENVIRONMENT
.venv-redteam/Scripts/python.exe -m pytest redteam/tests -q
.venv-redteam/Scripts/python.exe -m redteam.run --engine pyrit
.venv-redteam/Scripts/python.exe -m redteam.run --engine garak
```

Both tools have substantial dependencies, including machine-learning libraries.
Keep this environment out of the production Docker images. The backend remains
in `.venv`; PyRIT's dependencies differ from the Google ADK stack.

The default `smoke` mode uses `httpx.MockTransport`, sends no requests to the
configured target, and requires no model credentials. It tests real framework
execution and the API adapter against synthetic responses. Its outcome is
`smoke_only`, **never a security pass**.

The lightweight adapter tests also run in the backend environment:

```powershell
.venv/Scripts/python.exe -m pytest tests redteam/tests/test_client.py -q
```

The GitHub workflow runs these deterministic checks on relevant pull requests.
Its manual `workflow_dispatch` run also installs both frameworks and executes their
offline smoke tests. Neither CI job receives model credentials or runs live attacks.

## Browser GUI (CoPyRIT)

Double-click `Start-RedTeam-GUI.cmd` in the project root. It opens Microsoft's
bundled **CoPyRIT 1.1.0** at `http://127.0.0.1:8000` in offline smoke mode.
Keep the launcher window open; press Ctrl+C there to stop the server.
Alternatively, from the project root:

```powershell
.venv-redteam/Scripts/python.exe -m redteam.gui
```

In **Targets**, select `underwriting-smoke-independent-reports`, then open
**Chat**, start a new attack and enter a test prompt. The response will explicitly
say it is synthetic. **History** retains prompts and responses across restarts
in PyRIT's per-user SQLite database. The six project prompts are in `cases.json`.
Smoke results do not evaluate model safety.

For live testing, start the application separately, then launch:

```powershell
$env:REDTEAM_ALLOW_LIVE = "1"
$env:REDTEAM_ALLOWED_ORIGIN = "http://127.0.0.1:8080"
# Set REDTEAM_UI_TOKEN if your application requires it.
.venv-redteam/Scripts/python.exe -m redteam.gui --mode live --company-number YOUR_NUMBER --max-reports 6
```

Select `underwriting-live-independent-reports`. The company number, origin and
budget are fixed at startup. The budget is shared across all GUI attacks using
this target until the server restarts (default six, maximum fifty). Each prompt
performs a fresh resolve/approve/generate/reset cycle. Conversation history,
system messages and attachments are not supported by this application target;
start a new attack for each independent report. Errors are not successful defences.

The native Scanner may require additional targets/scorers and datasets depending
on the scenario. This integration provides the application target for CoPyRIT;
it does not import the project batch corpus or its custom scoring into Scanner.
The existing `redteam.run` commands remain the way to run the six-case PyRIT or
garak batch suite and produce its comparable JSON results. CoPyRIT does not run
garak. GUI chats are unscored unless you configure a suitable scorer.

This launcher binds only to loopback. The pinned GUI has no authentication when
Entra settings are absent: do not expose port 8000 publicly. For a GCP VM, access
it through an authenticated SSH/IAP tunnel. Live prompts and reports are stored
in PyRIT's per-user database and should follow the same retention rules as batch
results. `--port 8001` changes the local port; `--no-browser` suppresses auto-open.
The launcher does not load `.env` files automatically; pass credentials through
the environment. It uses the installed 1.1.0 backend API, not the newer
`pyrit_backend --auth-mode local` option shown in latest online documentation.

## Live batch campaigns

Start a separate test deployment with its own database, a rotated Companies House
key, and model credentials as described in the root README. Use an entity you are
authorized to research. Do not point the scanner at an existing user's session.

```powershell
$env:REDTEAM_ALLOW_LIVE = "1"
$env:REDTEAM_ALLOWED_ORIGIN = "http://127.0.0.1:8080"
# Set REDTEAM_UI_TOKEN from your secret store if the UI requires authentication.
.venv-redteam/Scripts/python.exe -m redteam.run --engine pyrit --mode live --base-url http://127.0.0.1:8080 --company-number YOUR_NUMBER --max-cases 2
.venv-redteam/Scripts/python.exe -m redteam.run --engine garak --mode live --base-url http://127.0.0.1:8080 --company-number YOUR_NUMBER --max-cases 2
```

Replace `YOUR_NUMBER` with an eight-character Companies House number. Remote
targets require HTTPS. The origin must exactly match `REDTEAM_ALLOWED_ORIGIN`;
redirects are rejected, and credentials cannot appear in the URL. The access token
comes from the environment, not command-line arguments or report metadata.

Each engine run has its own report-request budget (default six, maximum fifty),
runs sequentially, creates a new session/cookie jar per attack, approves only the
configured entity, and resets its session afterward. The framework wrapper does
not retry failed reports. The backend itself can retry and make multiple model
and registry calls; **the report budget is not a token or monetary budget**.
Set provider quotas and backend timeouts for a live campaign. The HTTP client uses
a 1,860-second timeout per request, allowing the local backend's 1,800-second
report deadline plus response overhead. Both the CLI and GUI accept `--timeout`
(1–7,200 seconds); set it above `REPORT_TIMEOUT_SEC` for longer backend deadlines.
A client timeout does not guarantee cancellation of server-side work.

## Coverage and scoring

`cases.json` contains six direct-input attacks: instruction override, company
switching, fabricated citations, suppression of uncertainty, role spoofing, and an
encoded instruction. PyRIT runs `PromptSendingAttack` against a custom `PromptTarget`.
garak runs its native `Probe` and `function.Single` generator against the same corpus.
Both use the same project-specific screening rules for comparable output.

JSON artifacts are written under ignored `redteam/results/` with tool versions,
case IDs, prompts, responses, counts, and framework errors. Outcomes:

| Outcome | Meaning |
| --- | --- |
| `smoke_only` | Synthetic adapter test; no model evaluated |
| `finding` | An attack marker, suspicious source URL, or company mismatch needs review |
| `not_observed` | No configured signal found; this does not establish safety |
| `error` | Target/setup/transport/cleanup failure; not a successful defence |

Exit status is **2** for incomplete/error runs, **1** for live findings, and **0**
otherwise. An HTTP 500 caused by report validation is still an error here: the
public API does not distinguish validation rejection from service failure, so the
suite cannot safely credit it as a blocked attack. PyRIT's native unscored outcome
is retained, but the suite does not mislabel it as an attack failure or a defence.

Review all live reports for the actual company researched, unsupported assertions,
source support, missing uncertainty, and instruction-following. A marker is only
a heuristic: a model may quote it while rejecting an attack, or comply with an
attack while omitting it. The deliberately fake domain uses `.invalid` and is not
a real exfiltration endpoint. Artifacts contain full reports and should be treated
as sensitive. Backend error bodies and access tokens are not included by the adapter;
model-generated text is retained verbatim and may itself contain sensitive content.
The frameworks also create their own per-user cache/log directories (including
PyRIT's application-data directory and garak's configuration/data directories).
Include those locations in retention and cleanup policies for live campaigns.

## Scope limits and next extensions

This initial suite evaluates **single-report, direct-input attacks**. It does not
yet poison actual web-search results or MCP responses, run adaptive multi-turn
attacks, use all built-in garak probes, or independently verify factual entailment.
The backend intentionally creates fresh model sessions for each report; presenting
it as a persistent chat target would produce misleading multi-turn results.

Existing backend regression tests cover authorization, cross-browser access,
approval races, and citation structure. Keep them as release gates. Use live
red-team results for investigation and reviewed regression cases, not an automatic
claim that the application is safe for underwriting decisions.

To extend coverage, add fixture-backed web/MCP poisoning in a separate staging
harness, then add provider-backed adversarial and judge models with explicit cost
limits. Do not add test-only injection endpoints to the production API.

## Framework references

- [Microsoft PyRIT](https://github.com/microsoft/PyRIT)
- [PyRIT 1.1 target contract](https://github.com/microsoft/PyRIT/blob/v1.1.0/pyrit/prompt_target/common/prompt_target.py)
- [PyRIT PromptSendingAttack](https://github.com/microsoft/PyRIT/blob/v1.1.0/pyrit/executor/attack/single_turn/prompt_sending.py)
- [NVIDIA garak](https://github.com/NVIDIA/garak)
- [garak function generator](https://github.com/NVIDIA/garak/blob/v0.17.1/garak/generators/function.py)
