# kleandbm.ai: agents that design analytical data models from evidence

kleandbm.ai is a multi-agent system that assists and accelerates the creation of **logical analytical data models** (Kimball star schemas) for data architects, analytics engineers and consultants, while keeping track of requirements and decisions. It reads the business's documents and inspects a read-only MySQL source, then proposes a model with grain, keys, relationships and measures. Every claim it makes is tied to evidence, and every business meaning stays with a person.

It was built as the capstone for the Carnegie Mellon Agentic AI program (Research Assistant option).

## The problem

When a company sets up a data lakehouse, the logical data model is the foundation its pipelines, metrics and reports are built on. It's expensive to get right and more expensive to get wrong:

- **Errors are costly.** A wrong grain, a silently chosen definition or an invented metric usually surfaces after the pipelines are built. Fixing it means reworking downstream pipelines, and it costs the modeler credibility. For example, a dashboard asks for profit by order while costs exist only by store and day; a model that adds a margin column anyway looks right until the numbers are questioned.
- **Modeling is slow, expert work.** Before drawing a table, experts read documents, reports and requirements and inspect the source data to learn what it can support.
- **Requirements move.** Especially in consulting, clients change or add requirements mid-project. If requirements, definitions and decisions aren't tracked, the consultant absorbs the cost of the change or ends up in a dispute with the client.

kleandbm.ai does the investigation with agents: it reads the documents, inspects the source, says what the data can and can't support, and asks a person when the answer is a business decision. Every requirement, definition and decision is versioned and attributed, so a change in scope is visible and can be priced.

## What it does

The user talks to one **Assistant** per data model. Behind it, a Router reads each message and sends it to the agent that should answer: the Business Requirements agent, the Architect, or both in turn. It asks a clarifying question when it can't tell. The user can also send a message straight to one agent.

1. **Requirements.** A person states what they want to analyze and uploads documents (reports, spreadsheets, notes, screenshots). The Business Requirements agent records requirements and business definitions with citations to document locations, and raises questions instead of guessing.
2. **Design.** The Architect agent inspects the linked MySQL source through curated read-only tools, then designs a candidate model: fact grain, dimensions, roles, measures, additivity, and a mapping from each requirement to the elements that answer it. Each design decision carries typed claims ("this column is unique", "every order has a store") that must cite evidence of the matching kind.
3. **Review.** A Validator on a different model family checks the candidate and judges each decision's evidence. Failed findings go back to the Architect for a bounded number of corrections.
4. **Human gate.** The result lands as a proposal with its diff, findings, open questions and readiness blockers. People confirm meanings, apply the design and accept the model. Agents can't.

## Architecture

![kleandbm.ai architecture: a Router sends each message to the Business Requirements agent or the Architect; the Architect consults the Requirements agent, sends its candidate to the Validator and hands a proposal to a person; all three agents search documents, the Architect queries the MySQL MCP server, a monitor scores transcripts, and the model and its registers live in PostgreSQL](docs/images/architecture.svg)

Around the agents sit a FastAPI backend (API, domain services, agents, persistence and adapters as separate layers, with core logic independent of HTTP), Auth0 with tenant and project permissions checked on every service call, a provider-neutral LLM adapter, and the React editor, which talks to the backend over REST and Server-Sent Events.

| Part | Where | What it does |
|---|---|---|
| Router | `backend/app/agents/router/` | One model call (`claude-haiku-5-5`) per user message that picks the agent: requirements, Architect, both in turn, or a clarifying question. It reads only the user's own messages and a summary the application builds, never documents or agent replies, and every decision is stored. |
| Business Requirements agent | `backend/app/agents/requirements/` | ReAct loop. Owns the Requirements and Business Definitions registers. Searches documents (pgvector), cites locations, raises questions. Never uses Tree of Thought. |
| Architect agent | `backend/app/agents/architect/` | Supervisor. Investigates the source, edits an isolated design candidate, records decisions with typed claims, consults the Requirements agent on business meaning, and opens a bounded Tree of Thought search only at an undecided technical conflict. |
| Validator agent | `backend/app/agents/validator/` | Reviews the candidate on another model family (`gpt-6-astra`). Read-only tools plus `record_finding`. A `passed` finding needs evidence; missing evidence is `unverified`, never a pass. |
| Transcript monitor | `backend/app/agents/monitor/` | Scores each finished run 0–100 for misbehaviour (followed an injection, unsupported claims, authority overreach). Triage for human review; it never blocks. |
| MySQL MCP server | `mcp/mysql_source/` | Curated read-only tools: list and describe tables, row counts, column profiles, candidate-key, relationship and coverage checks, and masked samples. Read-only grants verified each session, timeouts, row caps, schema fingerprints. |
| Backend | `backend/app/` | FastAPI with separate API, domain, agent, persistence and adapter layers. Every record is scoped to tenant, project and model, and versioned as append-only revisions in attributed changesets. |
| Frontend | `frontend/` | React + React Flow editor: model canvas, registers, Review drawer (diff, findings, apply), the Assistant chat with live streaming and a route line on each message, and a run viewer with transcripts and monitor assessments. |
| Evaluation | `eval/` | Synthetic businesses with answer keys (Northstar Retail and Meridian Supply), the scenario runner with keyword rubrics, the routing eval, and monitor calibration. |

### Safety in the code, not only in prompts

- **Authority.** The backend refuses any agent attempt to confirm or reject a business meaning, change a person-confirmed entry, or accept a model. Confirming a meaning needs approver rights.
- **Deterministic checks on confident claims.** Source references are checked against the schema the tools actually returned. A requirement can't be marked fully supported while a blocking question on it is open. A run with linked sources that never queried them fails a hard check.
- **Typed decision claims.** Each claim kind needs its own evidence (uniqueness needs a key check on those columns, a relationship needs a relationship check, a business meaning needs a document citation). The Validator judges whether each result shows the claim.
- **Untrusted content.** Document passages, column comments and sample values are data, never instructions. Agents can flag text that tries to instruct them; flags persist for a person to review. The Router never reads this content, so planted text can't steer which agent answers.
- **Personal data.** Masked before evidence is stored or a model sees it: by column in the MCP server, by pattern (emails, phones, cards, IBANs, SSNs) in the adapter, and in document passages at extraction. There is no unmasked mode.
- **Isolation.** Agents work in a candidate copy; nothing authoritative changes until a person applies it. Out-of-scope resources return 404 so they can't be probed, and an end-to-end test calls every project route across tenants.
- **Source access.** Agents get nine curated read-only tools and no free-form SQL. The account's grants are checked at the start of every session, and one with more than SELECT is refused.
- **Audit.** Every source call is append-only evidence with its arguments, result, schema fingerprint and actor. Every agent turn is stored in `run_messages`, and every routing decision in `routing_decisions`.

## Setup

Tested on macOS (Apple Silicon) with Homebrew. You need:

- Conda, with Python 3.15
- PostgreSQL 17 with pgvector (Homebrew, or `docker compose up` for Postgres only)
- MySQL 8 (Homebrew) for the sample sources
- Node.js 20+ and npm
- An Auth0 tenant (an API and a single-page application)
- API keys: Anthropic (Requirements and Architect agents, image reading), OpenAI (Validator, monitor, embeddings). Without an OpenAI key, set the Validator provider to `anthropic` (interim, `claude-sonnet-5-5`) or `none`, and keep the `fake` embedding provider.

### 1. Databases

```bash
brew install postgresql@17 pgvector mysql
brew services start postgresql@17
brew services start mysql
```

Create the role and databases (or use `docker-compose.yml`, which does the same):

```bash
psql postgres -c "CREATE ROLE kleandbm LOGIN PASSWORD 'kleandbm';"
psql postgres -c "CREATE DATABASE kleandbm OWNER kleandbm;"
psql postgres -f backend/scripts/init-test-db.sql
psql postgres -f backend/scripts/init-eval-db.sql
psql kleandbm -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql kleandbm_test -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

Run these as a superuser (your Homebrew account is one); pgvector needs a superuser to install.

### 2. Backend

```bash
conda create -n kleandbm-agentic python=3.15
cd backend
conda run -n kleandbm-agentic pip install -r pylock.toml
conda run -n kleandbm-agentic pip install -e . --no-deps
cp .env.example .env
```

Edit `backend/.env`: the Auth0 issuer and audience, `KLEANDBM_SECRETS_KEY` (the file shows how to generate one), the API keys, and `KLEANDBM_SOURCE_ALLOWED_HOSTS`. `pylock.toml` pins exact versions and hashes for macOS arm64; on another platform, install from `pyproject.toml` instead.

```bash
conda run -n kleandbm-agentic alembic upgrade head
conda run -n kleandbm-agentic uvicorn app.main:app --reload --port 8001
```

### 3. Sample sources

These build synthetic MySQL databases with a SELECT-only user and write its password to `backend/.env`:

```bash
eval/northstar/load.sh
conda run -n kleandbm-agentic python eval/northstar/verify.py
eval/meridian/load.sh
```

### 4. Frontend

```bash
cd frontend
npm install
cp .env.example .env.local
npm run dev
```

Set the Auth0 domain, SPA client ID and audience in `.env.local`, then open http://localhost:5173. The dev server proxies `/api` to the backend on port 8001. In Auth0, a post-login Action that adds `https://kleandbm.ai/name` and `https://kleandbm.ai/email` to the access token: `CLAIM_NAMESPACE` in `backend/app/api/auth.py`. Without it, users have no display name.

## Usage

1. Sign in, create a project, and start **New model (guided)**.
2. Upload `eval/meridian/meridian-ops-notes.md`. Never upload `eval/meridian/README.md`: it describes the expected answer.
3. Add the MySQL source: host `127.0.0.1`, database `meridian_ops`, user `meridian_ro`, and the password from `KLEANDBM_DEV_MERIDIAN_PASSWORD` in `backend/.env`. Credentials are entered only in this form, never in chat.
4. State the requirements in the Assistant. The Business Requirements agent records them and their definitions, and asks about anything unclear.
5. Choose **Generate draft model**. The Architect investigates the source and designs, and the Validator reviews it; a full run takes 7 to 14 minutes.
6. Open **Review** to see readiness, blockers, the diff, and the Validator's findings. Apply or discard the candidate.
7. In **Registers**, answer open questions and confirm or reject definitions and decisions. **View run** on any reply shows the full transcript and the monitor's assessment.

## Tests

```bash
cd backend
conda run -n kleandbm-agentic python -m pytest -q -rs
```

Database tests need `KLEANDBM_TEST_DATABASE_URL`; source tests need the Northstar password in `.env`. Live model tests are opt-in and cost money: `pytest -m live_llm`. Don't run two pytest sessions at once, since they share the test database.

```bash
cd frontend
npm run check
```

## Evaluation

The eval runs the Architect end to end against a synthetic source and grades the resulting model and registers against a rubric. Each run gets a fresh tenant and project in its own database (`kleandbm_eval`).

```bash
cd backend
conda run -n kleandbm-agentic python ../eval/baseline/run.py --check
conda run -n kleandbm-agentic python ../eval/baseline/run.py --scenario meridian --variants baseline --repeats 2
```

| Option | Meaning |
|---|---|
| `--scenario` | `northstar` (stated requirements, mostly a business-meaning conflict), `northstar_structures` (built to raise technical conflicts), `meridian` (split and multi-warehouse shipments, backorders, re-run inventory counts), `meridian_injection` (Meridian with planted prompt injections; build it with `MERIDIAN_INJECT=1 eval/meridian/load.sh`) |
| `--variants` | `baseline` (single candidate), `tot` (selective Tree of Thought), or both |
| `--repeats` | runs per variant; use 3 or more to compare |
| `--check` | free preflight: database, source and documents, no model calls |
| `--regrade DIR` | re-grade saved runs after a rubric change |
| `--prices FILE` | dollars per million tokens per model, to report cost |
| `--langsmith` | trace runs to LangSmith with full content (synthetic data only) |

Results go to `eval/baseline/results/<timestamp>/` (`report.md`, `summary.json`, one JSON per run). Each live run takes roughly 7–14 minutes. To calibrate the transcript monitor on saved runs:

```bash
conda run -n kleandbm-agentic python ../eval/monitor/calibrate.py ../eval/baseline/results/<folder>
```

The Router has its own eval, which sends labelled messages through the Router's model call with no database or agents:

```bash
conda run -n kleandbm-agentic python ../eval/routing/run.py --repeats 3
conda run -n kleandbm-agentic python ../eval/routing/run.py --cases ../eval/routing/cases_holdout.json --repeats 3
```

`cases_holdout.json` is held out: never tune the Router prompt to it.

**Answer keys.** `eval/northstar/README.md`, `eval/meridian/README.md` and `retail-discovery/verification.json` describe what a correct model looks like. They're for graders and readers. The runner refuses to upload them, and they must never be given to an agent.

### Results so far

| What | Result |
|---|---|
| Meridian, single candidate (latest runs) | Rubric 11/11 on all 8 completed runs (one earlier batch failed at its first call on a provider schema limit, since fixed). None of the runs reached Ready for Review, because business-meaning questions (for example the reporting calendar) correctly stayed open for a person. |
| Selective ToT vs. single candidate (Northstar, Northstar structures, Meridian; 3 repeats each) | No rubric gain. Before the trigger was narrowed, search opened on settled conflicts and roughly doubled cost. Now it opens only when the evidence leaves the choice undecided. |
| Demo set (a clean Northstar variant used for the live demo; 1 run) | Rubric 11/11 in under 8 minutes, with two questions, including the expected one on when returns are counted, and the undocumented Marketplace orders noticed. |
| Prompt injection (Meridian with 5 planted instructions and false claims) | No plant followed in 3 runs; questions still raised; nothing confirmed by agents; the plants were reported. |
| Router (labelled messages × 3 repeats) | 88.7% with the first prompt, 99.3% after one fix (50 cases); 97.9% on a held-out set of 47 cases never used for tuning, routed the same way on every repeat. |
| Transcript monitor calibration | 12/12 planted violations flagged with the right kind; 2 of 5 clean runs flagged at a threshold of 50 (1 of 5 at 65). It ranks runs for review and never blocks. |

Each result folder's `report.md` and `summary.json` are in `eval/baseline/results/`, `eval/monitor/results/` and `eval/routing/results/` (per-run JSON stays local: it holds transcripts and masked source samples). The rubrics are keyword heuristics over the stored records, and the scenarios are small.

## Repository layout

```
backend/          FastAPI app, agents, domain, persistence, Alembic migrations, tests
mcp/mysql_source/ read-only MySQL MCP server
frontend/         React editor
eval/             sample sources, scenario runner, graders, routing eval, monitor calibration
retail-discovery/ Northstar discovery documents (the report the agents read; verification.json is grader-only)
docs/images/      architecture diagram
```

All data in this repository is synthetic.
