"""Baseline runner: the Architect on Northstar with and without selective Tree of Thought.

    conda run -n kleandbm-agentic python eval/baseline/run.py --repeats 3
    conda run -n kleandbm-agentic python eval/baseline/run.py --scenario northstar_structures --repeats 3
    conda run -n kleandbm-agentic python eval/baseline/run.py --variants tot --repeats 1 --prices prices.json

Live and paid: every run calls the configured Architect, Business Requirements and Validator
models (about 10 minutes and roughly 20k-40k output tokens per run on Northstar). Needs the
Northstar MySQL source (eval/northstar/load.sh), KLEANDBM_ANTHROPIC_API_KEY, and
KLEANDBM_OPENAI_API_KEY for the Validator. Runs go into their own database
(KLEANDBM_EVAL_DATABASE_URL, default kleandbm_eval; create it with
backend/scripts/init-eval-db.sql), never the dev or test database, so it can run while pytest
does. Variants alternate (baseline, tot, baseline, ...) so drift over time affects both.

Writes eval/baseline/results/<timestamp>-<scenario>/: one JSON per run (records, usage, decision points,
rubric), summary.json and report.md. --prices takes {llm_model: {input, output, cache_read,
cache_write}} in dollars per million tokens; without it, cost is left blank rather than guessed.
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BACKEND = ROOT / "backend"
sys.path[:0] = [str(HERE), str(BACKEND)]
STARTED_IN = Path.cwd()  # relative paths on the command line mean this directory
os.chdir(BACKEND)  # settings read backend/.env

ISSUER = "https://eval.kleandbm.local/"
AUDIENCE = "https://api.kleandbm.eval"


def _source_password(name: str) -> str | None:
    if value := os.environ.get(name):
        return value
    env = BACKEND / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return None


def _regrade(folder: Path, prices) -> int:
    import grade
    import runner

    records = []
    for f in sorted(folder.glob("run-*.json")):
        record = json.loads(f.read_text())
        before = sum(g["passed"] for g in record["grade"].values())
        record["grade"] = grade.grade(record)
        f.write_text(json.dumps(record, indent=1, default=str))
        records.append(record)
        print(f"{f.name}: rubric {before} -> {sum(g['passed'] for g in record['grade'].values())}/{len(record['grade'])}")
    if not records:
        print(f"No run JSON in {folder}", file=sys.stderr)
        return 2
    summary = runner.summarize(records, prices)
    (folder / "summary.json").write_text(json.dumps(summary, indent=1))
    (folder / "report.md").write_text(runner.report(summary, records))
    print(f"Rewrote {folder / 'report.md'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--variants", default="baseline,tot", help="comma-separated: baseline, tot")
    parser.add_argument("--scenario", default="northstar", help="northstar, northstar_structures, meridian or meridian_injection")
    parser.add_argument("--repeats", type=int, default=1, help="runs per variant (LLM runs vary; 3+ to compare)")
    parser.add_argument("--prices", type=Path, help="JSON prices per model, dollars per million tokens")
    parser.add_argument("--out", type=Path, default=HERE / "results")
    parser.add_argument("--check", action="store_true",
                        help="set the scenario up once (database, source, documents) and stop before any agent runs")
    parser.add_argument("--regrade", type=Path, metavar="RESULTS_DIR",
                        help="re-grade a results folder's run JSON with the current rubric and rewrite its summary and "
                             "report (no agent runs, no database)")
    args = parser.parse_args()
    for name in ("prices", "out", "regrade"):
        if (value := getattr(args, name)) is not None and not value.is_absolute():
            setattr(args, name, STARTED_IN / value)
    if args.regrade:
        return _regrade(args.regrade, json.loads(args.prices.read_text()) if args.prices else None)

    import jwt
    from alembic import command
    from alembic.config import Config
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastapi.testclient import TestClient
    from langgraph.checkpoint.memory import InMemorySaver
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.adapters.sources import StdioMySqlConnector
    from app.adapters.storage import LocalObjectStore
    from app.api.auth import TokenVerifier
    from app.config import get_settings
    from app.main import create_app
    from app.persistence.db import get_session

    import runner
    from scenario import SCENARIOS

    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    bad = [v for v in variants if v not in runner.VARIANTS]
    if bad or args.repeats < 1 or args.scenario not in SCENARIOS:
        parser.error(f"unknown variants {bad}" if bad else "--repeats must be at least 1" if args.repeats < 1
                     else f"unknown scenario; use one of {sorted(SCENARIOS)}")
    chosen = SCENARIOS[args.scenario]
    settings = get_settings()
    password = _source_password(chosen.password_env)
    missing = [name for name, ok in (("KLEANDBM_ANTHROPIC_API_KEY", settings.anthropic_api_key),
                                     (f"the {chosen.database} source password ({chosen.password_env}; see eval/*/load.sh)", password)) if not ok]
    if missing:
        print(f"Missing: {', '.join(missing)}", file=sys.stderr)
        return 2
    if not settings.openai_api_key:
        print("Warning: no KLEANDBM_OPENAI_API_KEY, so the Validator is skipped and every design stays unverified.",
              file=sys.stderr)
    prices = json.loads(args.prices.read_text()) if args.prices else None

    url = os.environ.get("KLEANDBM_EVAL_DATABASE_URL") or settings.database_url.rsplit("/", 1)[0] + "/kleandbm_eval"
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.attributes["database_url"] = url
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    out = args.out / f"{datetime.now():%Y%m%d-%H%M%S}-{chosen.name}"
    out.mkdir(parents=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = TokenVerifier(issuer=ISSUER, audience=AUDIENCE, key_resolver=lambda _t: key.public_key())
    app = create_app(token_verifier=verifier, object_store=LocalObjectStore(out / "objects"),
                     session_factory=factory, agent_checkpointer=InMemorySaver(),
                     source_connector=StdioMySqlConnector(ROOT / "mcp", 5000, 30.0, 200_000))

    def _session():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_session] = _session
    subject = f"eval|{uuid.uuid4().hex[:12]}"

    with TestClient(app) as client:
        class User:
            def __init__(self) -> None:
                now = int(time.time())
                token = jwt.encode({"sub": subject, "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 4 * 3600,
                                    "name": "Baseline runner"}, key, algorithm="RS256")
                self.headers = {"Authorization": f"Bearer {token}"}
                client.get("/me", headers=self.headers)

            def get(self, url, **kw):
                return client.get(url, headers=self.headers, **kw)

            def post(self, url, **kw):
                return client.post(url, headers=self.headers, **kw)

            def put(self, url, **kw):
                return client.put(url, headers=self.headers, **kw)

        if args.check:
            from scenario import set_up
            ids = set_up(User(), chosen, password)
            print(f"Preflight OK: eval database migrated, {chosen.database} source verified read-only and linked, "
                  f"{len(chosen.documents)} document(s) uploaded, {len(chosen.requirements)} {chosen.name} requirements "
                  f"(project {ids['project']}). No agent was run.")
            return 0
        records = []
        plan = [v for _ in range(args.repeats) for v in variants]
        for i, variant in enumerate(plan, 1):
            print(f"[{i}/{len(plan)}] {variant} …", flush=True)
            record = runner.run_once(User(), chosen, variant, password, settings, fresh_user=User)
            records.append(record)
            (out / f"run-{i:02d}-{variant}.json").write_text(json.dumps(record, indent=1, default=str))
            passed = sum(g["passed"] for g in record["grade"].values())
            print(f"    {record['status']} · {record['state']} · {record['seconds']}s · rubric {passed}/{len(record['grade'])}"
                  f" · decision points {[d['status'] for d in record['decision_points']]}", flush=True)

    summary = runner.summarize(records, prices)
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    (out / "report.md").write_text(runner.report(summary, records))
    print(f"\nWrote {out / 'report.md'}")
    print(runner.report(summary, records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
