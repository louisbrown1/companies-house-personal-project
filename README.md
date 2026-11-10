# Underwriting research prototype

Python FastAPI UI, Google ADK research agents, and a Companies House MCP server.
The existing browser interface is retained; no React migration is needed.

## Setup

Use Python 3.12 and uv 0.13.7. Install uv using the [official instructions](https://docs.astral.sh/uv/getting-started/installation/). From the project root:

```powershell
uv sync --locked
```

Dependency declarations live in `pyproject.toml`; `uv.lock` pins the complete backend
resolution. Default sync includes developer tools and the local Gemma adapter.
Use `uv add PACKAGE` / `uv add --group dev PACKAGE` to change dependencies, then
review and commit both files. CI uses `--locked` to reject stale locks.

Gemma inference and red-team frameworks have independent projects and locks at
`runtimes/gemma` and `redteam`. Keep their environments in `.venv-gemma` and
`.venv-redteam` using the commands below; do not install those frameworks into
`.venv`. Stop services using an environment before syncing that environment.
On Windows ARM, the OCR/model dependencies currently use x64 Python under emulation:
use `uv sync --locked --python cpython-3.12-windows-x86_64-none` for a fresh backend.
Existing `.venv` interpreters are reused by ordinary `uv sync`.

Copy `.env.example` to `.env` and supply values locally. Never commit credentials.
The API key previously present in the notes was removed; revoke/rotate that key
in Companies House before using this application again.

Start the registry service in one terminal (set its API key in the environment):

```powershell
$env:MCP_TRANSPORT = "http"
$env:PORT = "8081"
.venv/Scripts/python.exe mcp_server/ch_mcp_server.py
```

Start the UI in another terminal:

```powershell
.venv/Scripts/python.exe -m uvicorn agent_ui.app:app --app-dir agent --env-file .env --port 8080
```

The UI uses HTTP MCP and requires `CH_MCP_URL` and `CH_MCP_MODE=http`. The standalone
agent also supports stdio via an absolute server path; override it with
`CH_MCP_SERVER_PATH` when the server is located elsewhere. Supply Google/Vertex
credentials through your usual application-default credentials or deployment identity.
`CH_MCP_AUTH`, if set, is forwarded as a bearer token to MCP by both clients;
the MCP deployment or its gateway must validate it. It does not enable server-side
authentication by itself.

Set `AGENT_UI_TOKEN` to require a shared access token. The browser prompts when needed
and keeps it in sessionStorage for that tab. PDF downloads use authenticated fetch.
An HttpOnly browser cookie binds session state to that browser; API clients must
retain cookies as well as sending `X-UI-TOKEN`. This is shared-token access, not
individual user accounts or an enterprise authorization system. Serve it over HTTPS.

## State and deployment

`DATABASE_URL` defaults to local SQLite (`underwriting.db`). Session candidates,
approvals and reports survive process restarts, expire after 24 hours, and use
optimistic version checks to reject stale operations. Each report attempt uses a
new, disposable ADK session; no prior-company conversation is reused.

For multiple hosts or Cloud Run, provide a shared PostgreSQL URL such as
`postgresql+psycopg://USER:PASSWORD@HOST/DATABASE`. The UI installs the psycopg driver.
Cloud Run startup rejects SQLite and requires `AGENT_UI_TOKEN`. Provision the database
and secrets before deployment. All instances must use the same database. Tables are
created on startup; for managed production rollout, provision the schema before
starting multiple instances simultaneously. SQLite is for local use, not shared
network storage. Expired rows are removed during subsequent session reads.

Build both images from the repository root so they use the committed lockfile:

```powershell
docker build -f agent/agent_ui/Dockerfile -t underwriting-ui .
docker build -f mcp_server/Dockerfile -t underwriting-mcp .
```

The UI image excludes development tools and the optional Gemma adapter; the MCP
image installs only the MCP dependency group. For a Gemma-backed UI image, add
`--group gemma` to its `uv sync` build step.
Keep the MCP service private or authenticated at its gateway.

## Report guarantees and limits

The browser starts report generation with a short background-job request and polls
the authenticated `/api/report-status` endpoint. Refreshing restores the approved
company and reconnects to a running job or saved report. Failures are persisted so
a disconnected browser can distinguish a timeout from a running job. Jobs run in
the serving process: this is not a durable worker queue, and a process restart
does not resume computation. Expired job reservations are reported as failures.

Financial extraction includes Gemma-assisted table interpretation. Gemma selects
source column IDs and row meanings through typed schemas; it cannot supply amounts.
The validator binds each accepted figure to a literal OCR cell, PDF hash, page,
reporting period, legal-entity/group scope and currency scale. Adjusted tables
require statutory Total columns. Low-confidence evidence and conflicting values
are withheld; cash-flow movements are not closing balances. Incomplete core
figures trigger a higher-resolution pass, retaining earlier validated facts and
quarantining disagreements across passes.

Regression replay: `redteam/results/run_table_interpretation.py` runs the actual
accounts workflow on frozen public filings with cached OCR and fresh table plans.
`redteam/results/score_table_interpretation.py` compares its results with 24
previously visually checked source anchors. This is a targeted regression check,
not exhaustive verification of every extracted figure or a cold-cache benchmark.

`DOCUMENT_MEMORY_ENABLED=1` enables experimental temporary document memory for
table interpretation. The default is off. A bounded Pydantic object holds source
row references, validated meanings and rejection reasons across batches and OCR
retries within one PDF. It is cleared in the filing's `finally` block; context
variables isolate concurrent requests. Memory cannot authorize a new amount or
override source validation. Memory-enabled interpretation bypasses saved table
plans so the current document context is actually evaluated.

Run `redteam/results/run_memory_comparison.py` for a sequential, alternating-order
comparison on the four frozen regression cases. Both arms disable table-plan
caching, use the same source artifacts and run in separate processes. Expected
answers are read only by the scorer after extraction. Results include accuracy
against 24 source anchors and accounts-workflow runtime; one paired run per
company cannot establish a reliable general speed improvement.

Approval must match a current search candidate. Legal names come from registry
results, not client input. The backend directly fetches the approved company pack
and seeds the research context, skipping LLM entity resolution and raw-data copying.
Changing approval, resetting or searching invalidates in-flight results. Concurrent
generation for the same unchanged session returns 409. Failed runs release their
generation reservation; abandoned reservations expire.

Each report request has a `REPORT_TIMEOUT_SEC` deadline (default 600 seconds), at
most three attempts, and at most 30 model calls per attempt. Only the synthesizer's
final response is accepted. Before saving, the backend validates evidence structure,
unique IDs, required report sections, and citation mappings. These checks do not
prove that a web source supports a claim; human review remains necessary.

The Companies House client retries transport failures, 429 and 5xx responses.
Other HTTP errors fail immediately. Only insolvency 404 means an absent record;
other failures stop report collection rather than presenting a clean result.

## Tests

### Local Gemma test profile

Install the optional backend adapter with
`uv sync --locked` (the Gemma adapter is included in the default groups).
Run the LiteRT-LM model server on loopback, then set these variables before
starting the underwriting API (or put them in its explicitly loaded env file):

```powershell
$env:MODEL_PROVIDER = "gemma"
$env:GEMMA_API_BASE = "http://127.0.0.1:9379/v1"
$env:GEMMA_MODEL = "gemma4-12b"
```

The model ID must match the local server's `/v1/models` response. All LLM agents
then use that endpoint through ADK's LiteLLM adapter; there is no Gemini fallback.
The existing approval flow, Companies House collection, evidence schema and report
validation still apply. For Gemma, the fetched registry pack is reduced to selected
company profile fields and API collection counts before it enters the model.
Individual officers, PSCs, charges, insolvency cases and filings are excluded;
this limitation is appended to every Gemma report in code. Source URLs and legal
entity identity are retained. Oversized selected context fails explicitly.
A Companies House API key and running MCP service are
still required for a real company report.

This initial Gemma profile skips web-review research and records an explicit
evidence gap: Gemini's built-in Google Search is incompatible with this endpoint.
It is a registry-focused test profile, not equivalent coverage to the Gemini
pipeline. Runtime tool-calling and structured-output compatibility must be verified
with the installed model before interpreting red-team results. Switching models
does not turn synthetic red-team smoke tests into live model tests.

Local runtime setup (separate environment):

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$PWD/.venv-gemma"
uv sync --project runtimes/gemma --locked --python .venv/Scripts/python.exe
Remove-Item Env:UV_PROJECT_ENVIRONMENT
.venv-gemma/Scripts/litert-lm.exe import --from-huggingface-repo=litert-community/gemma-4-12B-it-litert-lm gemma-4-12B-it.litertlm gemma4-12b
.venv-gemma/Scripts/litert-lm.exe serve --host 127.0.0.1 --port 9379
```

The model download is approximately 6.9 GB. Installation alone does not establish
inference compatibility or performance on Windows ARM.

To check real generation, the evidence JSON schema and tool-call output through
the same adapter used by the platform:

```powershell
$env:PYTHONPATH = "agent"
$env:MODEL_PROVIDER = "gemma"
.venv/Scripts/python.exe -m agent_ui.check_gemma
```

This writes `redteam/results/gemma-compatibility.json` and returns nonzero on a
failed or incomplete check. It uses synthetic prompts, performs no real company
lookup and does not establish report accuracy or red-team resilience.

On this Windows ARM laptop, LiteRT-LM 0.19.1 / Gemma 4 12B passed text generation,
the evidence schema and tool-call generation through ADK in CPU mode on 2026-11-07.
GPU initialization failed because a 480 MiB weights buffer exceeded the runtime's
256 MiB per-buffer limit. The tested model selector is
`gemma4-12b,cpu,4096`; the 4,096-token context is a compatibility-test setting,
not a validated capacity for full registry reports. Small checks took about
40–53 seconds each. Full-report performance and red-team resilience remain untested.
The full-company test exposed a 76,629-token raw registry prompt. The local `.env`
used `gemma4-12b,cpu,8192` for that test, with bounded registry context and a 1,800-second
report deadline. Individual Gemma calls have a 600-second timeout and thinking is
disabled for this local test profile. `.env.example` retains Gemini as default.
Gemma report attempts are not automatically retried: a timed-out request can still
be running in the local model server. Restart the model server to release that
work before another manual attempt.

The real-company baseline for 00519500 (TESCO STORES LIMITED) successfully
resolved and approved the entity and fetched registry data. After compacting the
input, the first research model call still exceeded the 600-second timeout on
this laptop's CPU. No report was generated, and no live red-team result can be
claimed from this run. See `redteam/results/gemma-platform-baseline.json` for the
local test record. The small compatibility checks therefore do not demonstrate
usable full-report performance for this hardware/model combination.
Double-click `Start-Gemma.cmd` to restart the model server later.

LiteLLM is pinned because the adapter uses its `_skip_mcp_handler` option to avoid
loading the optional MCP 2.x gateway. ADK executes MCP tools itself using the
application's MCP 1.x stack. Re-run the real tool-call check when upgrading it.

### Regression and red-team tests

The current local GUI profile uses `gemma4-e2b,gpu,8192`. The E2B GPU model
passed text, JSON and tool-call compatibility checks, but free-form multi-agent
reports produced unsupported competition estimates and invalid citations.
The local UI now uses a bounded registry-evidence workflow: Python retrieves
the approved company's registry data and a same-SIC/registered-office-location
search, constructs facts and source URLs, and renders the report and citations.
Gemma makes a schema-constrained call to select up to five existing evidence
IDs as general highlights, plus a separate financial-observation selection when
accounts figures are available. Unknown IDs or added model claims are rejected; repeated
highlight IDs are collapsed to one entry. Citations are generated once per fact.
Only API `hits`/`total_results` values supply the search count; unavailable
counts remain unknown. Counts are not converted into competition or credit
ratings. This explicitly limited report does not provide free-form business
analysis or a lending recommendation. User requests guide highlight
selection only. The Gemini workflow is unchanged.

With `TAVILY_API_KEY` in `.env`, the local workflow invokes an ADK FunctionTool
for two basic Tavily searches using only the public approved company name/number.
Maximum two results per query, deduplicated, with excerpts capped at 600 characters.
Generated answers and raw-page extraction are disabled; web search result URLs are
not fetched by this application. Each report normally uses two Tavily credits, with no automatic
search retries. Provider-reported usage and query status appear in the report.
Missing configuration and failed/partial searches remain explicit coverage gaps.
Search results are attributed candidate sources, not verified legal-entity matches
or company facts. Their snippets are passed to Gemma as untrusted data for highlight
selection; code owns citations. This does not provide full-page review, sentiment
analysis, or comprehensive adverse-media screening. Restart the UI after changing
the key. The key is never sent to Gemma or included in reports.

### Accounts: group and banking statements

Group-account filings now use an explicit scope/period-column extractor for
supported statements, including separate company/group balance sheets and
four-column cash-flow tables. Banking statements recognise interest income,
net interest income, customer loans and deposits, equity and wholesale funding.
Group and company facts and calculations remain separate. Same-filing
comparatives are labelled as such; they are not silently substituted for prior
filings. A bounded higher-resolution OCR retry handles difficult statement scans.
The Chetwood 2025 statements have regression fixtures for scope, signs and units.
Unsupported layouts and low-confidence cells remain unavailable. The latest banking
filing is scanned for selected notes, published regulatory capital and explicitly
scoped CET1, total-capital, leverage, LCR and NSFR ratios. Up to 90 notes pages and
six higher-resolution retries are bounded and cached. Published averages retain
their averaging window; movements are withheld for incompatible or unclear bases.
Capital amounts accept decimal GBP-million values separately from percentages.
Waiver mentions prompt source review rather than asserting approval or group scope.
Missing ratios remain explicit gaps; loan/deposit ratios are never substituted
for regulatory liquidity ratios. This is not an independent regulatory-compliance
assessment. Existing standalone-company extraction is retained.

### Executive summary

Local reports start with a six-bullet executive summary covering the assessed
entity, financial performance, liquidity, leases and group exposure, customer
ratings, and follow-up questions. It reuses Gemma-selected financial observations
and cited calculations with required topic coverage; no extra model call or
free-form factual generation is needed. Missing evidence, OCR limitations,
unverified legal-entity matches and potentially cached Tavily ratings remain
explicit. Detailed findings and source links follow the summary.

### Customer sentiment: platform ratings first

Each platform's published aggregate rating and exact review count are now the
primary review indicators, displayed before supporting individual excerpts. After
search discovery, the application fetches only allowed HTTPS review-profile URLs
(up to eight profiles, three concurrent requests, 20-second timeout, 2 MiB limit,
bounded same-provider redirects). No API credentials are sent to review sites.
Ratings are read deterministically from business JSON-LD `aggregateRating`, never
generated by Gemma or inferred from search snippets. An explicit rating scale and
exact review count are required; individual-review stars, product aggregates,
approximate counts, mismatched profiles and conflicting aggregates are withheld.

When Trustpilot or Yelp cannot be read directly, one Tavily Extract basic request
tries up to four discovered profile URLs. Only matching business metadata or an
explicit profile header with score, scale and exact count is accepted. Failed or
ambiguous extractions remain unavailable. The report labels this source as potentially
cached and shows extraction usage when returned by Tavily. No login is used. Date checked is
the retrieval time, not a claim that the provider updated its data then. Business
names are matched conservatively; the legal-entity link remains unverified. Yelp
location ratings remain separate from brand/business profiles. Scores are not
averaged across providers or converted to a credit score. Direct profile fetches
use no Tavily credits; the extraction fallback may incur additional credits.

The local Gemma workflow also searches **Trustpilot, REVIEWS.io, Feefo and Yelp**
separately through the Tavily ADK tool. Each provider gets one basic search with
restricted domains and at most two results, capped at 1,000 excerpt characters.
Returned URLs must be review-profile pages on the provider's domains; marketing
and policy pages are excluded. A conservative title-name check rejects unrelated
businesses, but does not verify the legal entity. These
four searches add up to four basic-search credits to the two background searches
(normally six credits per complete report); usage appears per provider. Authentication
or quota failures stop subsequent review searches. No login, direct platform API
or paid platform account is used. Public-profile metadata retrieval is described above.

The conservative passage filter retains short first-person passages and excludes
recognisable platform summaries and marketing. Gemma assesses each eligible result
independently, so one failure cannot suppress the other sources. It classifies
customer-experience text in the snippets as positive, negative,
mixed, neutral or insufficient. Non-insufficient classifications require an exact
supporting quote of 12–240 characters from that same excerpt. Gemma selects passage
IDs and code copies the original text, rather than asking the model to reproduce
quotes. Invalid model output
leaves sentiment insufficient with an explicit failure status. Star ratings,
review counts, company descriptions and platform marketing are not sufficient
customer-experience evidence. Quotes and classifications are model interpretations
requiring review, not verified company facts.

Reports keep providers separate, label legal-entity and branch/brand matches as
unverified, and distinguish empty searches from service failures. This is discovery
and preliminary snippet sentiment, not a representative full-review analysis.
No combined score, sentiment percentage, time trend or creditworthiness inference
is produced. Brand aliases are not invented; searching the approved legal name can
miss profiles listed under a different trading name. Missing results do not establish
that a company has no reviews. Full-review coverage needs separately verified
profiles and an authorised data-access method.

### Filed accounts and financial health (local Gemma)

The local report now retrieves the latest two distinct accounts reporting periods
from Companies House filing history, using the newest filing for a period where
an amendment exists. It downloads the linked PDFs through the Document API using
`COMPANIES_HOUSE_API_KEY`. Credentials are not forwarded to signed storage redirects.
Downloads are limited to 20 MiB and PDFs to 200 pages. Selection is bounded to the
latest 100 accounts filing records, with a coverage warning if more exist.

Run `uv sync --locked` to install the locked PDFium, RapidOCR and ONNX Runtime dependencies.
OCR runs locally on CPU; Gemma continues to use the configured GPU server. The
bundled OCR models do not require a cloud OCR service. PDF and OCR results are
cached in the ignored `.data/accounts/` directory. First reports take longer;
subsequent reports reuse document extraction. This cache contains public filings,
not API keys. It can be deleted while the application is stopped to reclaim space.

Extraction supports recognisable standalone-company, two-year statement tables,
with contents in the first six pages or short accounts of at most 16 pages. It
checks company name, reporting date, currency, monetary scale and OCR confidence.
Group/consolidated or ambiguous layouts, missing disclosures and failed downloads
remain explicit gaps. This is a deliberately limited extractor, not a universal
accounts parser. PDF statement page numbers and filing links accompany the figures.

Python computes operating margins, current ratios, net assets and supported
period-on-period changes. Growth is withheld unless the latest comparative agrees
with the previous filing. Parentheses retain their source sign; liability magnitudes
are used for ratios and net assets. Gemma selects up to five supported observations
from those calculations, without generating new numbers or a credit score.
Reports expose the filed figures even if the financial model selection fails.
The latest filing also receives a bounded notes scan (up to 49 pages around and
after the contents-listed notes start), with the first six pages checked for
management commentary. The first OCR scan can take several minutes on this laptop;
subsequent reports reuse the cached OCR. The previous filing supplies statement
comparisons; note comparisons use the latest filing's comparative columns.

The accounts-note section adds a reconciled operating-profit bridge, employee cost
details, adjusted versus statutory operating profit, lease carrying values, lease
cash payments and maturity schedules, net intragroup cash transfers, separately
reported dividends, funding balances and selected named counterparties. It quotes
management explanations with page references and flags group guarantees and cash
comparability disclosures where found. Management's explanation is attributed,
not independently established causality. Cost details overlap the statement bridge;
lease schedules are not added to recognised lease liabilities; funding balances
are not cash transfers. Blank cells and ambiguous layouts remain unknown. This is
selected-note analysis, not a complete audit, guarantee or covenant review.
OCR output still requires source review. The Gemini
multi-agent path is unchanged.

The B&M integration baseline uses **B&M Retail Limited, 01357507**, rather than
the parent group, and periods ended **28 March 2026** and **29 March 2025**.
Local test records are in `redteam/results/gemma-accounts-baseline.json` and
`redteam/results/bm-financial-extraction.json` (ignored, not committed).

For browser-based red teaming, double-click `Start-RedTeam-GUI.cmd` to open
CoPyRIT with the project's application target in offline smoke mode. See the
[GUI instructions](redteam/README.md#browser-gui-copyrit) for live testing.

An optional [PyRIT and garak red-team suite](redteam/README.md) runs in a separate
environment. Its default smoke mode is offline; model-backed campaigns require an
explicit target and opt-in. It supplements the regression tests below.

```powershell
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m pytest -q
```

Ruff is pinned in the `dev` group and configured in `pyproject.toml` for
Python 3.11+, import ordering, unused/undefined names, and core Python code checks.
The pull-request CI runs both Ruff checks before the deterministic tests.
To apply safe lint fixes and formatting locally, run:

```powershell
uv run --locked ruff check . --fix
uv run --locked ruff format .
```

Application, MCP server, test and red-team source files are checked. Virtual
environments, cached data, temporary files and generated `redteam/results` artifacts
are excluded. Ruff cache files are ignored by Git.

Frontend JavaScript uses ESLint with Node.js 24 or newer and npm installed. Inline scripts in
`agent/agent_ui/index.html`, standalone frontend JavaScript, and JavaScript tests
are checked using the browser or Node globals appropriate to each file:

```powershell
npm ci
npm run lint:js
npm run test:js
```

Use `npm run lint:js:fix` to apply safe automatic fixes. The frontend CI job runs
the same lint and test commands; warnings fail the lint check. Versions are pinned
in `package.json` and `package-lock.json`. A `minimatch` override keeps ESLint's
glob matcher on patched version 11.0.1 until its upstream dependency is updated.
The tests also verify that inline HTML scripts really receive lint diagnostics.

Tests use real API and database components with mocked external services. They
cover approval, browser ownership, authentication, PDF download, stale report races,
state persistence, retry classification, insolvency errors, authoritative company
identity, runtime cleanup and evidence/citation validation. They do not call live
Companies House or Gemini, or deploy cloud resources.

Accounts extraction now verifies the filing cover identity and period once, then
discovers statement sections from the contents page. Searchable PDFs use their
text layer; scanned PDFs use bounded OCR with a cover/statement retry. Page-aware
Markdown is saved alongside the original cell geometry under `.data/accounts`.
Bank and trading-company schemas constrain row interpretation. Gemma can map
source row IDs to permitted metric names; it cannot supply amounts, units, dates
or entity scope. Code copies those from validated source cells, rejects conflicts,
and checks available balance-sheet and net-interest totals with rounding tolerance.
Existing specialised extraction remains available for supported layouts and notes.

The local three-company integration runner is
`python -m redteam.results.run_layout_benchmark` (requires the GUI and configured
live providers). Results are saved in `redteam/results/layout-benchmark.json` and
individual company reports. It exercises Aldermore Bank PLC (00947662), Chetwood
Financial Limited (09964966), and B&M Retail Limited (01357507). These fixtures
test distinct layouts, not universal PDF coverage. Ambiguous identities, units,
dates, scopes and regulatory disclosures remain explicitly unavailable. Chetwood's
2024 cover currently fails the exact company-number check even after OCR retry;
comparatives from its verified 2025 filing remain usable.

### Local Open Knowledge Format context

The local Gemma workflow now consumes OKF v0.2 Markdown/YAML concepts from
`agent/agent_ui/knowledge/accounts/`. Common accounting context is always selected;
UK SIC 64191/64192 selects banking and division 47 selects retail. Banking
statements detected during extraction also select banking context. Other company
types receive common context without assuming they are retailers.

The concepts guide bounded row mapping and selection of existing financial
observations. They never supply company facts, execute code, fetch links, change
metric allowlists or replace Python calculations and source validation. No cloud
catalogue, vector database or additional model is needed.

The initial concepts describe the existing implementation and are agent-authored,
unverified advisory guidance. No human or independent accounting review is claimed.
The consumer skips draft, deprecated, stale, malformed or oversized concepts and
keeps deterministic extraction available. Each concept is limited to 8 KiB and
selected bodies to 6,000 characters. The initial review deadline is 7 February
2027; review the guidance before extending `stale_after`. Add `verified` entries
only after the named reviewer/process actually checks the content and sources.

Filing extraction diagnostics record selected concept IDs, content hashes, trust
signals and exclusions. Model mapping caches include the context manifest, so
concept or metadata changes invalidate cached interpretations. Source values
remain tied to their original PDF cells. The packaged files are included in the
existing application Docker image. Context consumer tests are in
`tests/test_okf_context.py`; the three-company live regression runner and source
anchor checks are `redteam.results.run_layout_benchmark` and
`redteam.results.check_layout_reports`.

### Temporary document evidence and discovered review profiles

Each hybrid extraction builds a separate, temporary OKF evidence bundle from
selected PDF pages and source-cell candidates. Gemma receives the bounded
candidate concept alongside the reusable company-type context. Document concepts
are draft/unverified evidence and cannot change the metric allowlist or validation
rules. A context manager removes the temporary directory on completion, model
failure or cancellation. Audit diagnostics retain the document/concept hashes,
page references and selected source facts; original PDF/OCR caches remain reusable.
New persistent page-Markdown bundles are no longer written.

Documents up to 600 pages are accepted within the existing 20 MiB download limit.
Statement discovery still reads at most six opening pages plus 24 statement
pages. Shared monetary units, group/company scope bands and space-separated
amounts are supported. Effective-interest income subtotals remain distinct from
total interest income when both appear. Note retrieval now uses Pydantic evidence
definitions and company-type OKF runbooks (`knowledge/navigation`). A bounded
heading index scans up to 200 note-page headers; these headers are navigation,
not full-page financial evidence. Gemma selects validated section IDs with reasons
and priorities. Retrieval reads at most 60 unique full note pages and eight pages
per section. Truncated ranges, skipped fields and indexing limits remain auditable.
For banks, missing validated CET1/LCR/NSFR values trigger one bounded alternative
selection round. Trading-company follow-ups also check for lease maturity
schedules, intercompany funding and employee-cost details, passing the specific
missing evidence back to the planner. Retrieval does not establish that a ratio
exists or is validated. Each batch requests structured JSON using a Pydantic-generated
schema, then validates types, indexed IDs, duplicate assignments and explicit field
coverage locally. Invalid, truncated or failed replies receive at most two retries
per batch per selection round (180 seconds per attempt). Successful batches are
preserved; an exhausted batch marks only its own fields unresolved. Conflicting
selected/unresolved assignments are rejected, not silently repaired. Audits retain
per-batch attempt outcomes and outstanding failures; invalid plans are never cached.
Bounded failed-response text is saved locally for diagnosis. The v6 plan cache
version refreshes selection under the updated field definitions and runbooks.
Related-party coverage includes a bounded companion-note check for observed headings
such as other assets, other receivables and other liabilities. It adds at most two
sections per field after primary selections, still within 60 unique pages and eight
pages per section. Every match, already-retrieved section and budget/section-limit
skip is recorded in the retrieval audit. These are candidate disclosures: neither
the heading nor retrieval establishes a related-party exposure or group funding.
Source date, scope, unit and numeric validation remain mandatory. Temporary OKF
navigation bundles are deleted; PDF/OCR, index, plan and audit caches remain.

Banking narrative excerpts are assembled within left-aligned text columns and
paragraph boundaries, separately from monetary table extraction. Low-confidence
text in a candidate excerpt is withheld and prioritised for the existing bounded
higher-resolution OCR retry (at most six pages). A retry that remains unreliable
produces an explicit evidence gap. Related-party section headings can supply
context for their following paragraphs, but are not published as standalone
disclosures. Table rows with aligned numeric cells are excluded before prose columns
are split, and contained excerpts are deduplicated. Malformed currency tokens
(such as a letter O inside a sterling amount) trigger source OCR retry even when
confidence is high; unresolved amounts are withheld rather than silently corrected.
The rules use source geometry and confidence, not issuer/page mappings.
Uncertain note text now receives a bounded crop fallback (six pages, forty lines
per page): local Windows OCR, when available, and RapidOCR image variants.
Monetary corrections require two matching image readings and cannot change
existing numeric values, units or surrounding wording. Severely garbled lines
require agreement between Windows OCR and RapidOCR. Original text, readings,
method, confidence and page geometry remain in the versioned retry cache.
Windows OCR requires an installed recognition language; other platforms retain
RapidOCR variants and withhold unresolved text. No document content is sent to an
external OCR service. Old retry caches are bypassed by the new cache version.


The sentiment workflow reuses review-profile candidates found by general web
search. Exact normalised name matches and bank-brand candidates are distinguished;
a short bank brand requires both a matching Trustpilot title and reviewed-domain
brand. This is explicitly not proof of legal-entity ownership. Aggregate ratings
still require independently fetched profile content with the business name,
explicit score/scale and exact review count. Search titles never supply a score.
Direct retrieval may fall back to Tavily Extract, labelled as potentially cached.

Run the four-company integration check with
`python -m redteam.results.run_temporary_bundle_reports`; it preserves separate
Investec, Aldermore, Chetwood and B&M report files and a benchmark JSON file under
`redteam/results/`.

Run the field-selection regression with
`python -m redteam.results.run_page_selection_reports`, then
`python -m redteam.results.check_page_selection_reports`. These retain separate
reports and compare all baseline financial facts as well as source anchors.

### Generic note extraction

Note extraction uses accounting labels and source scope rather than issuer names.
Company-only numeric notes conservatively exclude pages indicating consolidated
or wider-group tables. Verified document identity can replace repeated company
headers. Paired tables support either year order and explicit GBP units, thousands
or millions; reports convert source scales before calculations. Maturity extraction
accepts alternative captions and wording for the supported five-column schedule,
but requires ordered buckets, an explicit date/unit and a reconciled total.
Disclosure topic matching preserves quoted wording, including negation; topic
matches alone do not establish guarantees, support commitments or transfers.

`tests/test_generic_notes.py` exercises synthetic unfamiliar layouts and terminology.
`PYTHONPATH=agent python -m redteam.results.check_generic_notes` re-extracts the
four existing selected OCR sets, including cached high-resolution retries. This is
not an unseen-company or cold-cache benchmark. Other currencies, arbitrary maturity
bucket structures and ambiguous mixed-scope tables remain unsupported or withheld;
document navigation still has the bounded layout assumptions described above.

### Document discovery and identity validation

Statement discovery reads the first eight pages and pairs contents titles with
page-number cells on either side, including multiple columns and continuation
pages. Footer/header page-number anchors suggest the printed-to-PDF offset;
destination headings must confirm a proposed statement location. Contents-driven
discovery reads at most 40 additional full pages, with a fallback scan of up to
200 page headers and retrieval of up to 12 matching statement pages when no
statements are located. Cropped scans are never used as complete pages.
This remains bounded coverage, not a guarantee of finding every statement.
High-resolution retries reuse discovery for the same PDF hash and page count,
re-reading only the selected statement pages instead of repeating the heading scan.

Identity checks reconstruct adjacent legal-name lines and normalize numeric UK
registration numbers to eight digits while preserving alphabetic prefixes.
Registry-linked filings with unreadable numbers still require a matching source
legal name and dated statements. Explicit conflicting numbers, entities or periods
block extraction. Year-only covers require statement-date evidence; comparative
equity roll-forwards and later approval dates do not establish the current period.
Approval text beside a statement header is excluded too. Scope inheritance requires
a section heading; wrapped auditor-report cross-references cannot establish group
or company scope.
Each filing retains `document_identity` and `document_discovery` audit details.
Discovery and identity validation do not relax numeric column, unit or scope checks.

Joined OCR tokens in legal names and registration-number labels are normalised
without fuzzy entity matching. Failed identity checks retry the cover and the
actual contradictory statement headers at higher resolution (at most six pages);
contradictions still prevent extraction. Navigation includes indexed financial
reviews and risk sections, recognises `Note N` headings, and scans a larger upper
page region to handle large margins. Navigation and selection cache versions
invalidate older incomplete plans.

Banking ratios may also be copied from explicit, high-confidence narrative
paragraphs with a verified document, reporting date and stated scope. Columns
are kept separate. A disclosure referring only to the Bank retains an unspecified
regulatory perimeter rather than being relabelled company or group. Minimums,
targets, ambiguous scope and unsupported dates are withheld; missing NSFR is not
replaced by LCR or a balance-sheet proxy.


With `PYTHONPATH` set to `agent`, run a cold regression with
`python redteam/results/run_cold_extraction.py --case CASE --root NEW_DIRECTORY`, using
`starling`, `monzo`, `greggs` or `james_halstead`. Every case starts with an empty
PDF/OCR/model-mapping cache. After all four finish, run
`python redteam/results/check_cold_extraction.py --root NEW_DIRECTORY` to compare
against the 24 manually checked source anchors. These cases are now a frozen
regression set, not an unseen holdout. This command tests accounts extraction,
not the complete GUI, web-search and sentiment report workflow.
For an interrupted run, `--resume` reuses only that case's existing artifacts and
writes a separate resume manifest. Its results must be labelled as resumed
validation, rather than a new empty-cache run.


### Supplementary banking disclosures

Bank reports make at most two bounded Tavily searches for a matching-year Pillar 3
report, including a broader issuer search. Existing company web results seed
discovery. Up to six publisher pages and four PDFs are checked; anchor text and
serialized page-data links can identify disclosures whose filenames omit the year.
Brand-site links are candidates only: each destination must independently establish
the legal name and labelled company number (numeric leading zeros are normalized).
The PDF must explicitly identify the issuer and reporting period. Separate Bank
columns in a Group/Bank KM1 table require an explicit legal-name-to-Bank binding;
parent figures are not substituted. Single-year KM1 tables require an explicit
as-at date and retain an unspecified perimeter where the source does not define it.
Downloads require HTTPS/public addresses, stay on the same host (allowing only
www/non-www redirects), and are capped at 5 MB, 160 pages and 120 seconds.
This initial supplementary reader supports explicit text-based ratio tables;
scanned or unsupported layouts remain unavailable rather than being guessed.
Pillar 3 values retain their own source citations, PDF hash, dates and averaging
windows. Year-end LCR is distinct from a 12-month-average LCR; NSFR's four-quarter
average is labelled separately. Missing averaging information is explicitly labelled
unspecified and is not used to calculate comparative movements. Document name/number matching is provenance
validation, not independent verification of the publisher's claims or compliance.

Registration-number validation distinguishes company registration labels from
FCA/PRA, charity and tax references. Generic registration labels are accepted on
opening identity pages; elsewhere a company-specific label is required. Actual
conflicting company numbers remain a veto, including errors printed in the
source document. No digit substitution is made to force a match.
