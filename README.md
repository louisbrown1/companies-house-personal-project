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

Table interpretation v9 also recognises printed `£'m` units and `FY25`-style
column headings. Abbreviated years must map uniquely to an allowed reporting
period and agree with an explicit statement day/month; bare two-digit numbers
are not treated as years. Compact OCR contents headings can locate financial
statements and auditor context without scanning the whole report. Company scope
still requires explicit issuer-only evidence, and a high-resolution amount can
retain a stronger literal currency header from the same PDF's standard OCR pass.
Auditor context searches a bounded neighbourhood around the contents reference.
A missing comparative cell does not discard an independently validated current
cell; damaged combined liabilities-and-equity labels cannot establish equity.
Earlier table-plan cache versions are not reused under these rules.

All Tavily research stages in a full report share one request budget: background
search, review discovery, Pillar 3 discovery, peer discovery and page extraction.
`SEARCH_MAX_REQUESTS_PER_REPORT` defaults to 40 search calls and
`SEARCH_MAX_EXTRACT_REQUESTS_PER_REPORT` to 12 extraction calls. These count
outbound requests, not provider credits or a guaranteed currency spending cap.
Authentication and quota errors stop further outbound Tavily calls in that report;
valid cached responses remain usable. Limits and usage are included in report gaps.

Successful Tavily responses are cached in `.data/research-cache.sqlite3` for
`SEARCH_CACHE_TTL_SECONDS` (default 3600; 0 disables caching). Extraction responses
expire after at most 900 seconds. Cache keys include request parameters and a
hashed account identifier; API keys are not stored. Original retrieval timestamps
are preserved, stale responses are never used after errors, and the cache is
bounded to 1,000 entries with a maximum one-day lifetime. Concurrent reports have
independent budgets. Standalone tool calls retain their existing individual limits.

Google Search grounding is not currently a Tavily replacement in this pipeline.
Its [grounding terms](https://aistudio.google.com/docs/terms#grounding-with-google-search)
restrict reusable caching and automated collection of links for crawling, which
conflicts with the source-discovery/extraction workflow here. A separate
user-facing grounded research view would need its associated search suggestions
and must not feed the reusable discovery cache. Local Gemma configuration is
unchanged.

An experimental browser fallback can recover issuer identity pages and Pillar 3
discovery links when API discovery fails or returns no usable sources. Install
with `uv sync --frozen --group browser`, then set `BROWSER_RESEARCH_ENABLED=1`.
The default `BROWSER_RESEARCH_CHANNEL=msedge` uses an installed Microsoft Edge;
other environments can use `chromium` after `uv run --group browser playwright
install chromium`. The browser uses an isolated, headless profile, not a user's
signed-in browser. `BROWSER_RESEARCH_SEARCH_ENGINE` selects `bing` (default) or
`duckduckgo`; an access challenge stops the run without switching engines.

Gemma must use a localhost endpoint. It selects observed link IDs through a typed
schema; it cannot invent addresses or execute scripts. This is DOM/text-based
navigation, not unrestricted visual computer control. Code stops when the issuer
identity goal is established. Browser pages and links remain candidates: existing
registration-number, publisher, reporting-period and PDF-metric validation still
control report facts. Direct Companies House retrieval is unchanged. General peer
candidate search and review-site discovery still require the search provider.

Limits default to two browser runs per report, six page steps and 120 seconds per
run (`BROWSER_RESEARCH_MAX_RUNS`, `BROWSER_RESEARCH_MAX_STEPS`,
`BROWSER_RESEARCH_TIMEOUT_SEC`). Each page reserves up to 250 subresource requests,
with a whole-run cap of 250 times the page-step limit, and the run
reserves a separate navigation allowance of four requests per page step (including
redirects and frames). It blocks private addresses, non-HTTPS navigation, form submissions, downloads
and service workers, and closes its browser on completion. Issuer pages have an
eight-second bounded wait for rendered content. CAPTCHA, HTTP blocks,
timeouts and incomplete discovery are explicit gaps. Verified issuer URL hints
expire after one hour in the bounded research cache; pages and PDFs are fetched
and validated again on every run. Cached hints never supply report evidence.
Literal issuer URLs in validated accounts also provide discovery candidates with
page/hash provenance. Gemma sees a shortlist of relevant observed links, with
its choices recorded in the navigation trace. Serialized PDF links behind download
buttons are read without executing their handlers. Externally hosted PDFs require
an explicit link from a verified issuer page as well as document validation.
Cold discovery waits up to eight seconds for organic results, then broadens an
empty result through at most three disclosure/legal-name/issuer queries within
the same navigation and time budgets. Result-specific legal-name/CRN snippets
prioritise links over similarly named businesses; they never verify identity.
Fragment and empty-root-path variants count as the same visited page. The report
research audit records whether each browser run started from search, a supplied
source or a cached issuer hint. Challenges stop discovery without another query.
Clearly older Pillar 3 archive links do not end a current-period search while
relevant landing pages remain. Publisher identity also accepts a legal-name-bound
"company registered in [UK jurisdiction] (No. ...)" statement; regulator numbers
and other entities' registration statements cannot satisfy this binding.
The direct disclosure downloader accepts PDFs up to 25 MB, while HTML remains
limited to 5 MB. Both declared length and streamed bytes are checked; the existing
160-page and overall retrieval-time limits still apply.
Discovery still depends on usable search results; engines or publishers may block
automation. For cold-discovery regression runs, give each bank a new
`SEARCH_CACHE_PATH`; this isolates search responses and issuer URL hints, while
leaving the separate accounts/OCR cache unchanged.

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

Gemma and Gemini use the same validated workflow: registry context, Tavily search,
verified review aggregates, accounts/OCR and supplementary Pillar 3 discovery.
The provider controls model inference, not evidence coverage. Model outputs select
source-backed IDs or constrained extraction fields; code validates evidence,
calculates financial observations and renders citations. Synthetic red-team tests
still do not establish live model accuracy.

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
selection only. Gemini uses this same workflow.

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
OCR output still requires source review. Both providers use this extraction path.

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


Typed evidence migration: financial facts, regulatory measurements, banking
note disclosures and aggregate review ratings are validated with Pydantic at the
EvidenceItem boundary. Existing structured_fact dictionaries and Markdown output
retain their wire format and source provenance; typed_fact exposes the validated
view. Other registry/search evidence remains on the legacy contract during this
first migration stage. Typed validation does not replace source verification.
Banking missing-metric, conflict and transcription gaps also retain structured
records. Supplementary evidence resolves missing gaps by metric, period and
requested scope, never display-message wording; conflicts remain open. A null gap
scope means evidence is missing across all scopes, not that scopes are equivalent.
Resolved gap records are retained in EvidenceBundle.gaps for auditing; existing
uncertainty strings remain the presentation compatibility layer.

### Shared agent contract

`agent/agent.py` is an ADK adapter for `agent_ui/workflow.py`, also used by the UI.
Without approval it returns registry candidates and `approval_required`; an ADK
host must set `session.state["approved_company"]` after user selection, containing
`company_number` and optionally `company_name`. Prompt text cannot approve or
replace that entity. The fetched registry identity must match the approved number.
The UI already persists approval separately from prompts.

`RegistrySettings` validates MCP mode, URL and timeouts. Stdio always launches the
child with `MCP_TRANSPORT=stdio`, even if the parent serves HTTP. HTTP bearer auth
and read timeouts are shared across entry points. Toolsets are constructed only
when explicitly requested; normal orchestration uses bounded FastMCP calls.
Evidence requires exactly one payload matching its claim. Unknown free-form claims
are rejected; financial facts, paired notes, regulatory ratios, disclosures and
published ratings have typed validation. Registry/search metadata remains explicitly
allowlisted. Existing OKF runbooks provide document-selection guidance; executable
rules own approval, collection, validation and citations. No runtime monkey patches
are introduced by this adapter.

### Financial peer comparison

Reports now include a **Financial peer comparison** section. Packaged sector
runbooks live in `agent/agent_ui/knowledge/peers/`; Pydantic contracts are in
`peer_models.py` and `peer_discovery_models.py`. Web discovery is in `peer_discovery.py`;
financial validation and calculations are in `peer_comparison.py`.
The runbooks contain validated policy limits and bounded guidance for model selection
of existing comparison IDs. There are no company names, registration numbers or
issuer-specific page numbers in these runbooks.

Automatic selection first builds a business profile from issuer pages bound to the
subject's registration number. Tavily searches for candidate businesses; Gemma selects
statement IDs and copies candidate names with source IDs under Pydantic schemas. Code verifies names occur in those sources and retains the
exact quotes and URLs. Search snippets propose leads but cannot establish a business
profile or financial figure. Candidates need issuer-page registration-number binding,
an active Companies House identity, and supported overlap in products, customers and
operating geography. Ambiguous parent/subsidiary mappings and subject aliases are rejected.
Staff biographies, legal boilerplate and registered offices are not business evidence.
Selection interpretations and inclusion/exclusion reasons appear in the report.

Web discovery has a 240-second ceiling. Candidate names are extracted from up to four
small source batches, so an empty or invalid batch cannot discard other leads. Up to
eight distinct business names undergo identity and profile verification. Exact registry
name matches may guide a targeted registration-number search, but only issuer-page
evidence establishes the identity. Missing business dimensions trigger one bounded
follow-up search. Equivalent `www`/bare-host extraction URLs are accepted; unrelated
hosts and different page paths are not interchangeable.
It never substitutes SIC-only candidates if search or model validation fails. Financial
validation attempts at most three accounts extractions to retain up to two peers.
Filings outside the 93-day comparison window are rejected with their available dates
shown, without consuming a document extraction attempt or widening that window.
Identity, active status and SIC are rechecked; dormant accounts are excluded. Banks need
positive company loans and customer deposits; retail/trading candidates need revenue
and assets. Company assets at the compared periods must be supported and within the
runbook's default 10x size limit. These checks do **not** establish regulatory
authorisation, direct competition or independent ownership. Missing evidence remains a gap.

Bank benchmarks support loans/deposits, accounting equity/assets, CET1, LCR and NSFR
when extracted evidence supports a comparison. Retail/trading benchmarks support
current ratio and equity/assets. Code validates source units, scale, scope, dates,
denominators, conflicts, regulatory basis and averaging windows. Differences between
percentages are percentage points. Unknown regulatory bases are withheld. The subject
latest filing is compared with the nearest eligible peer period among its two latest filing records. A single eligible peer document is selected before OCR,
with both dates and the separation shown; the maximum allowed separation is 93 days.
If no latest-period benchmark is possible, a whole historical subject period may be compared, explicitly labelled as historical rather than current performance. Individual metrics are never backfilled from another year. Profitability and growth comparisons
are withheld pending validated period lengths and accounting definitions. Peer extraction skips targeted detailed-note retrieval and extra Pillar 3
website discovery; regulatory metrics present in statement context may be used, while missing regulatory comparisons remain gaps.

The peer stage has a maximum 600-second budget, reduced to fit the remaining report deadline with time reserved for rendering. It is skipped when no time remains; completed peers survive a later failure or timeout. Optional model highlight selection is also bounded. The first run can be substantially slower because peer documents are newly
extracted. Use `REPORT_TIMEOUT_SEC=1800` to accommodate the primary report plus this
stage. Primary company evidence remains available if optional peer comparison fails.
The bounded sample is not representative: no sector average, ranking, market share,
competition score or lending recommendation is calculated. Sources and pages for
both sides of every calculation are retained and displayed; OCR still needs review.

For a deliberately chosen benchmark set, set `PEER_COMPANY_NUMBERS` to at most two
comma-separated CRNs. Leave it blank for automatic discovery. Explicit selections
still undergo registry identity, active status, SIC, sector-account and calculation
validation; the subject excludes itself. Reports disclose whether selection was
explicit or automatic. A rejected explicit peer is not silently replaced by an
automatically discovered company.
