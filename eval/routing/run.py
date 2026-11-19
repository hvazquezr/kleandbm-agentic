"""Router accuracy on the labelled set in cases.json, or on the held-out set:

    conda run -n kleandbm-agentic python eval/routing/run.py --cases eval/routing/cases_holdout.json --repeats 3

Other uses:

    conda run -n kleandbm-agentic python eval/routing/run.py              # one pass, live
    conda run -n kleandbm-agentic python eval/routing/run.py --repeats 3  # consistency too
    conda run -n kleandbm-agentic python eval/routing/run.py --check      # validate cases, no model call
    conda run -n kleandbm-agentic python eval/routing/run.py --rereport eval/routing/results/<ts>

Live and paid, but small: one call to the router model (KLEANDBM_ROUTER_AGENT_*, default
claude-haiku-5-5 at low effort) per case and repeat, with no database, documents or agents.
Writes eval/routing/results/<timestamp>/: results.json (every call), summary.json and report.md.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1] / "backend"
STARTED_IN = Path.cwd()  # relative paths on the command line mean this directory
sys.path[:0] = [str(HERE), str(BACKEND)]
os.chdir(BACKEND)  # settings read backend/.env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--cases", type=Path, default=None, help="another cases file (default: cases.json)")
    parser.add_argument("--check", action="store_true", help="validate the cases and print a brief; no model call")
    parser.add_argument("--rereport", type=Path, help="rewrite summary.json and report.md from a results folder")
    args = parser.parse_args()

    import routing_eval

    if args.rereport:
        folder = (STARTED_IN / args.rereport).resolve()
        results = json.loads((folder / "results.json").read_text())
        summary = routing_eval.summarize(results)
        (folder / "summary.json").write_text(json.dumps(summary, indent=1))
        (folder / "report.md").write_text(routing_eval.report(summary, results))
        print(f"Rewrote {folder / 'report.md'}")
        return 0

    data = routing_eval.load((STARTED_IN / args.cases).resolve() if args.cases else routing_eval.CASES)
    routes = sorted({c["expect"][0] for c in data["cases"]})
    print(f"{len(data['cases'])} cases, intended routes: "
          + ", ".join(f"{r} {sum(c['expect'][0] == r for c in data['cases'])}" for r in routes))
    if args.check:
        print("\nExample brief (" + data["cases"][-7]["id"] + "):\n" + routing_eval.case_brief(data, data["cases"][-7]))
        print("\nCases OK. No model was called.")
        return 0

    from app.config import get_settings
    from app.main import _chat_model_from_settings

    model = _chat_model_from_settings(get_settings(), "router")
    folder = HERE / "results" / datetime.now().strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True)

    def progress(row):
        mark = "ok  " if row["correct"] else "MISS"
        print(f"{mark} {row['id']:<10} r{row['repeat']} {row['route'] or 'error'} ({row['seconds']} s)")

    results = routing_eval.run_cases(model, data, args.repeats, progress=progress)
    summary = routing_eval.summarize(results, model.model)
    (folder / "results.json").write_text(json.dumps(results, indent=1))
    (folder / "summary.json").write_text(json.dumps(summary, indent=1))
    (folder / "report.md").write_text(routing_eval.report(summary, results))
    print(f"\nAccuracy {summary['accuracy']}% (strict {summary['strict_accuracy']}%) on {summary['calls']} calls. "
          f"Report: {folder / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
