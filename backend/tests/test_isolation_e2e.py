"""End-to-end isolation (P6): two tenants, several users and projects, every project route.

Each project holds one of everything: a model with tables and a version, register entries,
a document, a source with evidence, a conversation with an agent run, a design candidate and a
decision point. Then every route is called with every way of reaching the wrong data, and must
answer 404, exactly like a resource that doesn't exist. A positive control calls the same
routes in scope, so a route that 404s for everyone can't make the test pass.

Users:
- alice owns tenant A and its projects A1 and A2.
- bob owns tenant B and its project B1.
- dave belongs to both tenants and to projects A1 and B1, but not to A2. He's the one who can
  mix ids: the case that catches a dropped tenant or project filter.
"""

import json
import re
import uuid

import pytest
from cryptography.fernet import Fernet

from app.adapters.llm import ScriptedChatModel
from app.config import get_settings
from app.domain import design, tenancy
from app.domain.access import Actor, ActorKind
from app.persistence.conversations import ContentFlag, RunAssessment
from app.persistence.search import DecisionPoint, SearchStatus, Trigger
from tests.test_sources import FakeConnector

SOURCE = {"name": "ops", "host": "127.0.0.1", "port": 3306, "database": "ops", "username": "ro", "password": "x"}

# (method, path under the project base, JSON body). Ids are filled from the target project.
ROUTES: list[tuple[str, str, dict | None]] = [
    ("GET", "", None),
    ("PATCH", "", {"description": "probe"}),
    ("GET", "/models/{model}/snapshot", None),
    ("GET", "/models/{model}/versions", None),
    ("POST", "/models/{model}/versions", {"name": "probe"}),
    ("GET", "/models/{model}/state", None),
    ("POST", "/models/{model}/state", {"state": "ready_for_review"}),
    ("GET", "/models/{model}/state/history", None),
    ("GET", "/models/{model}/definition-dependencies", None),
    ("PUT", "/models/{model}/layout", {"positions": [{"record_id": "{table}", "x": 1, "y": 2}]}),
    ("GET", "/models/{model}/candidates", None),
    ("GET", "/models/{model}/sources", None),
    ("PUT", "/models/{model}/sources/{source}", None),
    ("DELETE", "/models/{model}/sources/{source}", None),
    ("GET", "/records/{record}", None),
    ("GET", "/records/{record}/history", None),
    ("GET", "/records/{record}/impact", None),
    ("GET", "/records/{table}", None),
    ("GET", "/documents/{document}/versions", None),
    ("GET", "/document-versions/{version}/content", None),
    ("GET", "/document-versions/{version}/passages", None),
    ("POST", "/document-versions/{version}/reembed", None),
    ("POST", "/document-versions/{version}/reprocess", None),
    ("PATCH", "/sources/{source}", {"name": "probe"}),
    ("POST", "/sources/{source}/test", None),
    ("GET", "/sources/{source}/evidence", None),
    ("POST", "/sources/{source}/tools/list_tables", {"arguments": {}}),
    ("POST", "/sources/{source}/disable", None),
    ("GET", "/evidence/{evidence}", None),
    ("GET", "/conversations/{conversation}/messages", None),
    ("POST", "/conversations/{conversation}/messages", {"text": "probe", "attachment_version_ids": []}),
    ("GET", "/agent-runs/{run}", None),
    ("GET", "/agent-runs/{run}/messages", None),
    ("GET", "/agent-runs/{run}/assessments", None),
    ("POST", "/agent-runs/{run}/assessments/{assessment}/reviews", {"verdict": "false_alarm"}),
    ("GET", "/conversations/{conversation}/assessments", None),
    ("GET", "/content-flags", None),
    ("POST", "/content-flags/{flag}/review", {"note": "probe"}),
    ("GET", "/candidates/{candidate}", None),
    ("GET", "/candidates/{candidate}/decision-points", None),
    ("POST", "/candidates/{candidate}/apply", None),
    ("POST", "/candidates/{candidate}/discard", None),
    ("POST", "/decision-points/{decision_point}/resolve", {"outcome": "rejected"}),
    # Writes that name another project's records from inside your own project.
    ("POST", "/changesets", {"operations": [
        {"op": "update", "id": "{record}", "expected_seq": 1, "data": {"statement": "probe"}}]}),
    ("POST", "/changesets", {"model_id": "{model}", "operations": [
        {"op": "delete", "id": "{table}", "expected_seq": 1}]}),
    ("POST", "/changesets", {"model_id": "{model}", "operations": [
        {"op": "create", "type": "attribute", "data": {
            "table_id": "{table}", "name": "probe", "logical_type": "text"}}]}),
    ("POST", "/models", {"name": "probe"}),
]

# Routes that only take the project, so they matter for the project-level cases only.
PROJECT_ONLY = {i for i, (_, path, body) in enumerate(ROUTES) if not re.search(r"\{[a-z_]+\}", path + json.dumps(body))}


def _fill(value, ids: dict[str, str]):
    """Put the target project's ids into a path or body template."""
    text = json.dumps(value) if not isinstance(value, str) else value
    for key, v in ids.items():
        text = text.replace("{" + key + "}", v)
    return text if isinstance(value, str) else json.loads(text)


@pytest.fixture
def world(as_user, client, db_session_factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "secrets_key", Fernet.generate_key().decode())
    client.app.state.source_connector = FakeConnector()
    client.app.state.chat_model_factory = lambda *_: ScriptedChatModel([])  # replies with text only

    alice, bob, dave = as_user("auth0|alice"), as_user("auth0|bob"), as_user("auth0|dave")
    ta = alice.post("/tenants", json={"name": "Acme"}).json()["id"]
    tb = bob.post("/tenants", json={"name": "Globex"}).json()["id"]
    a1 = alice.post(f"/tenants/{ta}/projects", json={"name": "Sales"}).json()["id"]
    a2 = alice.post(f"/tenants/{ta}/projects", json={"name": "Finance"}).json()["id"]
    b1 = bob.post(f"/tenants/{tb}/projects", json={"name": "Ops"}).json()["id"]
    for owner, tenant, project in [(alice, ta, a1), (bob, tb, b1)]:
        assert owner.put(f"/tenants/{tenant}/members/{dave.id}", json={"role": "member"}).status_code == 204
        assert owner.put(f"/tenants/{tenant}/projects/{project}/members/{dave.id}",
                         json={"role": "owner"}).status_code == 204

    def populate(owner, tenant: str, project: str, marker: str) -> dict[str, str]:
        base = f"/tenants/{tenant}/projects/{project}"
        model = owner.post(f"{base}/models", json={"name": f"Model {marker}"}).json()["id"]
        created = owner.post(f"{base}/changesets", json={"model_id": model, "operations": [
            {"op": "create", "type": "requirement", "ref": "r", "data": {
                "statement": f"Revenue by store ({marker})", "origin_kind": "user_statement"}},
            {"op": "create", "type": "table", "ref": "t", "data": {
                "name": f"fact_{marker}", "kind": "fact"}},
        ]}).json()["created"]
        doc = owner.post(f"{base}/documents", files={
            "file": (f"{marker}.txt", f"Glossary {marker}: zebra{marker} means net revenue.".encode(), "text/plain")}).json()
        source = owner.post(f"{base}/sources", json=SOURCE).json()["id"]
        assert owner.put(f"{base}/models/{model}/sources/{source}").status_code == 204
        evidence = owner.post(f"{base}/sources/{source}/tools/list_tables", json={"arguments": {}}).json()["id"]
        conversation = owner.post(f"{base}/conversations", json={"model_id": model}).json()["id"]
        resp = owner.post(f"{base}/conversations/{conversation}/messages",
                          json={"text": "hello", "attachment_version_ids": [], "agent": "business_requirements"})
        run = next(json.loads(l[6:]) for l in resp.text.splitlines() if l.startswith("data: "))["run_id"]
        with db_session_factory() as session:
            scope = tenancy.resolve_project_scope(session, Actor(ActorKind.USER, owner.id),
                                                  uuid.UUID(tenant), uuid.UUID(project))
            candidate = design.start_candidate(session, scope, uuid.UUID(model), uuid.UUID(run))
            dp = DecisionPoint(tenant_id=scope.tenant_id, project_id=scope.project_id, model_id=candidate.model_id,
                               candidate_id=candidate.id, run_id=uuid.UUID(run), trigger=Trigger.GRAIN_CONFLICT,
                               question="Order or order line grain?", scope={"elements": []},
                               status=SearchStatus.PARTIAL)
            assessment = RunAssessment(tenant_id=scope.tenant_id, project_id=scope.project_id, run_id=uuid.UUID(run),
                                       status="completed", score=10, flagged=False, summary="ok", concerns=[],
                                       untrusted_instructions=[], samples=[10], input_tokens=0, output_tokens=0,
                                       truncated=False)
            flag = ContentFlag(tenant_id=scope.tenant_id, project_id=scope.project_id, run_id=uuid.UUID(run),
                               agent="architect", target_kind="source", source_id=uuid.UUID(source),
                               source_table="orders", quote="ignore previous instructions", reason="addresses agents")
            session.add_all([dp, assessment, flag])
            session.commit()
            candidate_id, dp_id, assessment_id, flag_id = str(candidate.id), str(dp.id), str(assessment.id), str(flag.id)
        return {"base": base, "model": model, "record": created["r"], "table": created["t"],
                "document": doc["id"], "version": doc["latest"]["id"], "source": source, "evidence": evidence,
                "conversation": conversation, "run": run, "candidate": candidate_id, "decision_point": dp_id,
                "assessment": assessment_id, "flag": flag_id}

    return dict(alice=alice, bob=bob, dave=dave, ta=ta, tb=tb, a1=a1, a2=a2, b1=b1,
                A1=populate(alice, ta, a1, "a1"), A2=populate(alice, ta, a2, "a2"), B1=populate(bob, tb, b1, "b1"))


def _call(user, method: str, path: str, body):
    send = {"GET": user.get, "POST": user.post, "PUT": user.put, "PATCH": user.patch, "DELETE": user.delete}[method]
    return send(path) if body is None else send(path, json=body)


def test_every_project_route_is_isolated(world):
    w = world
    A1, A2, B1 = w["A1"], w["A2"], w["B1"]
    cases = [
        # (who, base path, ids of the resources asked for, why it must fail)
        (w["bob"], A1["base"], A1, "another tenant's project"),
        (w["dave"], A2["base"], A2, "a project in his tenant he isn't a member of"),
        (w["dave"], A1["base"], B1, "his own project with ids from his other tenant"),
        (w["dave"], B1["base"], A1, "his other tenant's project with these ids"),
        (w["dave"], A1["base"], A2, "his own project with ids from a sibling project"),
        (w["dave"], f"/tenants/{w['ta']}/projects/{w['b1']}", B1, "tenant A with a tenant B project"),
        (w["dave"], f"/tenants/{w['tb']}/projects/{w['a1']}", A1, "tenant B with a tenant A project"),
    ]
    leaks = []
    for user, base, ids, why in cases:
        project_level = ids["base"] != base and base in (A1["base"], B1["base"])
        # The same calls with ids that don't exist anywhere: a foreign id must look exactly like these.
        unknown = {k: (v if k == "base" else str(uuid.uuid4())) for k, v in ids.items()}
        for i, (method, path, body) in enumerate(ROUTES):
            if project_level and i in PROJECT_ONLY:
                continue  # the caller may use their own project's collection routes
            r = _call(user, method, base + _fill(path, ids), None if body is None else _fill(body, ids))
            expected = 404
            if project_level and path == "/changesets":
                # Inside a changeset an unknown record is an invalid operation (409), never a 404.
                ref = _call(user, method, base + path, _fill(body, unknown))
                expected = ref.status_code
                if r.status_code == expected and r.json().get("error") != ref.json().get("error"):
                    leaks.append(f"{method} {path} [{why}] differs from an unknown id: {r.text[:120]}")
            if r.status_code != expected:
                leaks.append(f"{method} {path} [{why}] -> {r.status_code} (want {expected}) {r.text[:120]}")
    assert not leaks, "\n".join(leaks)

    # Positive control: in scope, the same routes don't 404 (so the 404s above mean something).
    # Run last, since some of them change state.
    for owner, ids in [(w["alice"], A1), (w["bob"], B1)]:
        for method, path, body in ROUTES:
            if path.startswith("/changesets") or path.endswith("/discard") or path.endswith("/disable") \
                    or (method == "DELETE"):
                continue  # writes that would destroy fixtures the other checks still use
            r = _call(owner, method, ids["base"] + _fill(path, ids), None if body is None else _fill(body, ids))
            assert r.status_code != 404, f"{method} {path} in scope -> 404 {r.text[:160]}"


def test_listings_and_search_only_show_the_callers_project(world):
    w = world
    A1, B1 = w["A1"], w["B1"]
    dave = w["dave"]
    # dave sees both his projects' models only through their own project, never mixed.
    a1_models = {m["id"] for m in dave.get(f"{A1['base']}/models").json()}
    assert A1["model"] in a1_models and B1["model"] not in a1_models and w["A2"]["model"] not in a1_models
    assert {d["id"] for d in dave.get(f"{A1['base']}/documents").json()} == {A1["document"]}
    assert {s["id"] for s in dave.get(f"{A1['base']}/sources").json()} == {A1["source"]}
    convs = dave.get(f"{A1['base']}/conversations").json()
    assert {c["id"] for c in convs} == {A1["conversation"]}

    # Search returns only this project's passages, even for a word only another project has.
    hits = dave.post(f"{A1['base']}/search", json={"query": "zebrab1 net revenue", "limit": 20}).json()
    assert hits and all(h["document_id"] == A1["document"] for h in hits)
    assert not any("zebrab1" in h["text"] for h in hits)

    # Tenants and projects listed to a user are only their own.
    assert {t["id"] for t in w["bob"].get("/tenants").json()} == {w["tb"]}
    assert {p["id"] for p in dave.get(f"/tenants/{w['ta']}/projects").json()} == {w["a1"]}
