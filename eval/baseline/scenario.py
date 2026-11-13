"""The Northstar scenario as agents see it: the read-only source, the discovery workbook and
three stated requirements. Nothing from eval/northstar/README.md (the answer key) or
retail-discovery/verification.json ever goes into a project; the grader alone knows them."""

from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass(frozen=True)
class Scenario:
    name: str
    requirements: list[str]
    documents: list[Path] = field(default_factory=list)
    database: str = "northstar_ops"
    username: str = "northstar_ro"
    prompt: str = "Design the model."


NORTHSTAR = Scenario(
    name="northstar",
    requirements=[
        "Monthly net revenue by channel, store and product category",
        "Monthly returns by channel, store and product category",
        "Profit by order",
    ],
    # Only the workbook. The answer key and verification.json are grader-side only.
    documents=[ROOT / "retail-discovery" / "northstar-current-state-report.xlsx"],
)

FORBIDDEN = {"README.md", "verification.json"}


def set_up(user, scenario: Scenario, password: str, *, host: str = "127.0.0.1", port: int = 3306) -> dict:
    """Create a fresh tenant, project and model for one run, link the source, upload the
    documents and record the requirements. Returns the paths and ids the run needs."""
    for doc in scenario.documents:
        if doc.name in FORBIDDEN:
            raise ValueError(f"{doc.name} is grader-side only and must never be uploaded")
    tenant = _ok(user.post("/tenants", json={"name": f"Eval {scenario.name}"}))["id"]
    project = _ok(user.post(f"/tenants/{tenant}/projects", json={"name": scenario.name}))["id"]
    base = f"/tenants/{tenant}/projects/{project}"
    model = _ok(user.post(f"{base}/models", json={"name": scenario.name}))["id"]
    source = _ok(user.post(f"{base}/sources", json={
        "name": f"{scenario.name} source", "host": host, "port": port, "database": scenario.database,
        "username": scenario.username, "password": password}))["id"]
    r = user.put(f"{base}/models/{model}/sources/{source}")
    assert r.status_code == 204, r.text
    for doc in scenario.documents:
        _ok(user.post(f"{base}/documents", files={"file": (doc.name, doc.read_bytes(), XLSX)}))
    _ok(user.post(f"{base}/changesets", json={"operations": [
        {"op": "create", "type": "requirement", "data": {"statement": s, "origin_kind": "user_statement"}}
        for s in scenario.requirements]}))
    conv = _ok(user.post(f"{base}/conversations", json={"model_id": model, "agent": "architect"}))["id"]
    return {"tenant": tenant, "project": project, "base": base, "model": model, "conversation": conv}


def _ok(r):
    assert r.status_code in (200, 201), f"{r.request.method} {r.request.url}: {r.status_code} {r.text}"
    return r.json()
