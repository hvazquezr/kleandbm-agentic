"""Versioned model elements, registers, links, layout, versions, and lifecycle state."""

import uuid

import pytest

from app.domain import modeling, tenancy
from app.domain.access import Actor, ActorKind
from app.domain.modeling import CreateOp


@pytest.fixture
def proj(as_user):
    """Alice owns a project in her tenant with one data model."""
    alice = as_user("auth0|alice")
    tenant = alice.post("/tenants", json={"name": "Acme"}).json()["id"]
    project = alice.post(f"/tenants/{tenant}/projects", json={"name": "Sales"}).json()["id"]
    base = f"/tenants/{tenant}/projects/{project}"
    model = alice.post(f"{base}/models", json={"name": "Retail"}).json()["id"]

    class P:
        pass

    p = P()
    p.alice, p.tenant, p.project, p.base, p.model = alice, tenant, project, base, model

    def cs(user, operations, model_id=model, reason=None):
        body = {"model_id": model_id, "operations": operations}
        if reason:
            body["reason"] = reason
        return user.post(f"{base}/changesets", json=body)

    def snap(user=None, **params):
        return (user or alice).get(f"{base}/models/{model}/snapshot", params=params).json()

    p.cs, p.snap = cs, snap
    return p


def star_schema_ops():
    return [
        {"op": "create", "type": "table", "ref": "dim", "data": {"name": "dim_customer", "kind": "dimension"}},
        {"op": "create", "type": "attribute", "ref": "dim_key",
         "data": {"table_id": "$dim", "name": "customer_key", "logical_type": "integer", "role": "surrogate_key", "is_nullable": False}},
        {"op": "create", "type": "table", "ref": "fact",
         "data": {"name": "fact_sales", "kind": "fact", "grain": "One row per order line"}},
        {"op": "create", "type": "attribute", "ref": "fk",
         "data": {"table_id": "$fact", "name": "customer_key", "logical_type": "integer", "role": "foreign_key"}},
        {"op": "create", "type": "attribute", "ref": "amount",
         "data": {"table_id": "$fact", "name": "sales_amount", "logical_type": "decimal", "role": "measure", "additivity": "additive"}},
        {"op": "create", "type": "relationship", "ref": "rel",
         "data": {"from_table_id": "$fact", "from_attribute_id": "$fk", "to_table_id": "$dim", "to_attribute_id": "$dim_key"}},
    ]


def _by_name(snapshot, key="tables"):
    return {r["data"]["name"]: r for r in snapshot[key]}


# --- Changesets and versioning -------------------------------------------------------------


def test_create_star_schema_in_one_changeset(proj):
    resp = proj.cs(proj.alice, star_schema_ops(), reason="initial draft")
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["seq"] == 1
    assert set(out["created"]) == {"dim", "dim_key", "fact", "fk", "amount", "rel"}

    s = proj.snap()
    assert s["current_seq"] == s["as_of_seq"] == 1
    assert set(_by_name(s)) == {"dim_customer", "fact_sales"}
    assert len(s["attributes"]) == 3 and len(s["relationships"]) == 1
    rel = s["relationships"][0]["data"]
    assert rel["cardinality"] == "many_to_one"
    assert rel["from_table_id"] == out["created"]["fact"]


def test_update_requires_and_checks_expected_seq(proj):
    created = proj.cs(proj.alice, star_schema_ops()).json()["created"]
    fact = created["fact"]

    missing = proj.cs(proj.alice, [{"op": "update", "id": fact, "data": {"name": "fact_order_lines"}}])
    assert missing.status_code == 409 and missing.json()["error"] == "invalid_operation"

    ok = proj.cs(proj.alice, [{"op": "update", "id": fact, "expected_seq": 1, "data": {"name": "fact_order_lines"}}])
    assert ok.status_code == 201

    # A second writer still holding seq 1 must not silently overwrite.
    stale = proj.cs(proj.alice, [{"op": "update", "id": fact, "expected_seq": 1, "data": {"grain": "x"}}])
    assert stale.status_code == 409
    assert stale.json()["error"] == "stale_update" and stale.json()["current_seq"] == 2

    table = _by_name(proj.snap())["fact_order_lines"]
    assert table["seq"] == 2 and table["data"]["grain"] == "One row per order line"


def test_snapshot_as_of_returns_earlier_state(proj):
    created = proj.cs(proj.alice, star_schema_ops()).json()["created"]
    proj.cs(proj.alice, [{"op": "update", "id": created["dim"], "expected_seq": 1, "data": {"name": "dim_client"}}])
    proj.cs(proj.alice, [{"op": "delete", "id": created["amount"], "expected_seq": 1}])

    assert "dim_client" in _by_name(proj.snap())
    assert "dim_customer" in _by_name(proj.snap(as_of=1))
    assert "sales_amount" in _by_name(proj.snap(as_of=2), "attributes")
    assert "sales_amount" not in _by_name(proj.snap(), "attributes")
    assert proj.alice.get(f"{proj.base}/models/{proj.model}/snapshot", params={"as_of": 99}).status_code == 409


def test_history_is_append_only_and_attributed(proj):
    created = proj.cs(proj.alice, star_schema_ops(), reason="initial").json()["created"]
    proj.cs(proj.alice, [{"op": "update", "id": created["fact"], "expected_seq": 1, "data": {"description": "Sales"}}], reason="describe")
    history = proj.alice.get(f"{proj.base}/records/{created['fact']}/history").json()
    assert [h["seq"] for h in history] == [1, 2]
    assert [h["reason"] for h in history] == ["initial", "describe"]
    assert history[0]["data"]["description"] is None and history[1]["data"]["description"] == "Sales"
    assert all(h["actor_kind"] == "user" and h["actor_id"] == str(proj.alice.id) for h in history)


def test_failed_operation_rolls_back_whole_changeset(proj):
    ops = star_schema_ops() + [{"op": "update", "id": str(uuid.uuid4()), "expected_seq": 1, "data": {}}]
    resp = proj.cs(proj.alice, ops)
    assert resp.status_code == 409 and "operation 6" in resp.json()["detail"]
    s = proj.snap()
    assert s["current_seq"] == 0 and s["tables"] == []


def test_delete_table_cascades_to_attributes_relationships_links_and_layout(proj):
    created = proj.cs(proj.alice, star_schema_ops()).json()["created"]
    req = proj.cs(proj.alice, [
        {"op": "create", "type": "requirement", "ref": "r", "data": {"statement": "Sales by customer", "origin_kind": "user_statement"}},
        {"op": "link", "from_id": "$r", "to_id": created["dim"], "link_type": "maps_to"},
    ]).json()
    proj.alice.put(f"{proj.base}/models/{proj.model}/layout",
                   json={"positions": [{"record_id": created["dim"], "x": 10, "y": 20}]})

    assert proj.cs(proj.alice, [{"op": "delete", "id": created["dim"], "expected_seq": 1}]).status_code == 201
    s = proj.snap()
    assert set(_by_name(s)) == {"fact_sales"}
    assert set(_by_name(s, "attributes")) == {"customer_key", "sales_amount"}  # the fact's attributes
    assert s["relationships"] == [] and s["links"] == [] and s["layout"] == []
    # The requirement itself survives; only its link to the deleted table ends.
    assert [r["id"] for r in s["requirements"]] == [req["created"]["r"]]
    assert len(proj.snap(as_of=2)["links"]) == 1


def test_reference_rules(proj):
    created = proj.cs(proj.alice, star_schema_ops()).json()["created"]
    # The relationship's attribute must belong to the table it names.
    bad_rel = proj.cs(proj.alice, [{"op": "create", "type": "relationship", "data": {
        "from_table_id": created["fact"], "from_attribute_id": created["dim_key"],
        "to_table_id": created["dim"], "to_attribute_id": created["dim_key"]}}])
    assert bad_rel.status_code == 409 and "does not belong" in bad_rel.json()["detail"]
    # A reference must point at the right record type.
    wrong_type = proj.cs(proj.alice, [{"op": "create", "type": "attribute", "data": {
        "table_id": created["fk"], "name": "x", "logical_type": "text"}}])
    assert wrong_type.status_code == 409
    # Elements can't reference another model in the same project.
    other = proj.alice.post(f"{proj.base}/models", json={"name": "Other"}).json()["id"]
    cross = proj.cs(proj.alice, [{"op": "create", "type": "attribute", "data": {
        "table_id": created["fact"], "name": "x", "logical_type": "text"}}], model_id=other)
    assert cross.status_code == 409 and "same model" in cross.json()["detail"]
    # Model elements need a model on the changeset; payloads reject unknown fields and physical types.
    assert proj.cs(proj.alice, star_schema_ops()[:1], model_id=None).status_code == 409
    assert proj.cs(proj.alice, [{"op": "create", "type": "table", "data": {"name": "t", "kind": "fact", "rows": 5}}]).status_code == 409
    assert proj.cs(proj.alice, [{"op": "create", "type": "attribute", "data": {
        "table_id": created["fact"], "name": "x", "logical_type": "VARCHAR(255)"}}]).status_code == 409


# --- Registers and links --------------------------------------------------------------------


def test_register_codes_links_and_metric_details(proj):
    out = proj.cs(proj.alice, [
        {"op": "create", "type": "requirement", "ref": "r1", "data": {"statement": "Net revenue by month", "origin_kind": "dashboard", "origin_ref": "exec dashboard p2"}},
        {"op": "create", "type": "requirement", "ref": "r2", "data": {"statement": "Profit by order", "origin_kind": "user_statement"}},
        {"op": "create", "type": "definition", "ref": "d", "data": {
            "term": "Net revenue", "kind": "metric", "provenance": "documented",
            "metric": {"formula": "gross - returns"}}},
        {"op": "link", "from_id": "$r1", "to_id": "$d", "link_type": "uses_definition"},
    ], model_id=None)
    assert out.status_code == 201, out.text
    s = proj.snap()
    assert sorted(r["code"] for r in s["requirements"]) == ["REQ-001", "REQ-002"]
    definition = s["definitions"][0]
    assert definition["code"] == "DEF-001"
    assert definition["data"]["confirmation_status"] == "unconfirmed"
    # Details nobody supplied stay explicitly unresolved rather than invented.
    assert definition["data"]["metric"]["time_basis"] is None
    assert len(s["links"]) == 1

    link_id = s["links"][0]["id"]
    dup = proj.cs(proj.alice, [{"op": "link", "from_id": out.json()["created"]["r1"], "to_id": out.json()["created"]["d"], "link_type": "uses_definition"}], model_id=None)
    assert dup.status_code == 409
    wrong_pair = proj.cs(proj.alice, [{"op": "link", "from_id": out.json()["created"]["d"], "to_id": out.json()["created"]["r1"], "link_type": "maps_to"}], model_id=None)
    assert wrong_pair.status_code == 409
    assert proj.cs(proj.alice, [{"op": "unlink", "link_id": link_id}], model_id=None).status_code == 201
    assert proj.snap()["links"] == []


def test_impact_follows_links_and_structure(proj):
    created = proj.cs(proj.alice, star_schema_ops()).json()["created"]
    reg = proj.cs(proj.alice, [
        {"op": "create", "type": "definition", "ref": "d", "data": {"term": "Sales amount", "kind": "metric", "provenance": "user_stated"}},
        {"op": "create", "type": "requirement", "ref": "r", "data": {"statement": "Sales by customer", "origin_kind": "user_statement"}},
        {"op": "link", "from_id": "$r", "to_id": "$d", "link_type": "uses_definition"},
        {"op": "link", "from_id": "$r", "to_id": created["fact"], "link_type": "maps_to"},
    ], model_id=None).json()["created"]

    entries = proj.alice.get(f"{proj.base}/records/{reg['d']}/impact", params={"depth": 2}).json()
    found = {e["record"]["id"]: (e["depth"], e["via"]) for e in entries}
    assert found[reg["r"]] == (1, "uses_definition")
    assert found[created["fact"]] == (2, "maps_to")
    assert created["dim"] not in found  # three hops away

    deeper = proj.alice.get(f"{proj.base}/records/{reg['d']}/impact", params={"depth": 3}).json()
    assert {e["record"]["id"] for e in deeper} >= {created["fk"], created["amount"], created["rel"]}


# --- Layout, versions, lifecycle ------------------------------------------------------------


def test_layout_is_cosmetic(proj):
    created = proj.cs(proj.alice, star_schema_ops()).json()["created"]
    url = f"{proj.base}/models/{proj.model}/layout"
    assert proj.alice.put(url, json={"positions": [{"record_id": created["fact"], "x": 1.5, "y": 2}]}).status_code == 204
    assert proj.alice.put(url, json={"positions": [{"record_id": created["fact"], "x": 3, "y": 4}]}).status_code == 204
    s = proj.snap()
    assert s["current_seq"] == 1  # no changeset for layout
    assert s["layout"] == [{"record_id": created["fact"], "x": 3.0, "y": 4.0}]
    assert proj.alice.put(url, json={"positions": [{"record_id": created["fk"], "x": 0, "y": 0}]}).status_code == 409


def test_named_versions_label_watermarks(proj):
    vurl = f"{proj.base}/models/{proj.model}/versions"
    assert proj.alice.post(vurl, json={"name": "empty"}).status_code == 409
    created = proj.cs(proj.alice, star_schema_ops()).json()["created"]
    v1 = proj.alice.post(vurl, json={"name": "First draft"}).json()
    proj.cs(proj.alice, [{"op": "delete", "id": created["fact"], "expected_seq": 1}])
    assert v1["seq"] == 1
    assert "fact_sales" in _by_name(proj.snap(as_of=v1["seq"]))
    assert [v["name"] for v in proj.alice.get(vurl).json()] == ["First draft"]


def test_acceptance_needs_approval_and_lapses_on_change(proj, as_user):
    carol = as_user("auth0|carol")
    proj.alice.put(f"/tenants/{proj.tenant}/members/{carol.id}", json={"role": "member"})
    proj.alice.put(f"{proj.base}/members/{carol.id}", json={"role": "editor"})
    proj.cs(proj.alice, star_schema_ops())
    surl = f"{proj.base}/models/{proj.model}/state"

    assert proj.alice.get(surl).json()["effective_state"] == "draft"
    assert carol.post(surl, json={"state": "ready_for_review"}).status_code == 200
    denied = carol.post(surl, json={"state": "accepted"})
    assert denied.status_code == 403 and denied.json()["required_permission"] == "project:approve"

    accepted = proj.alice.post(surl, json={"state": "accepted", "note": "Approved for build"}).json()
    assert accepted == {"recorded_state": "accepted", "recorded_at_seq": 1,
                        "effective_state": "accepted", "changed_since_state": False}

    # A register change counts too: definitions can invalidate an accepted model.
    proj.cs(carol, [{"op": "create", "type": "definition", "data": {"term": "Customer", "provenance": "inferred"}}], model_id=None)
    lapsed = proj.alice.get(surl).json()
    assert lapsed["recorded_state"] == "accepted" and lapsed["recorded_at_seq"] == 1
    assert lapsed["effective_state"] == "draft" and lapsed["changed_since_state"] is True


# --- Scope and permissions ------------------------------------------------------------------


def test_modeling_is_tenant_and_role_scoped(proj, as_user):
    bob = as_user("auth0|bob")
    created = proj.cs(proj.alice, star_schema_ops()).json()["created"]
    assert bob.get(f"{proj.base}/models/{proj.model}/snapshot").status_code == 404
    assert bob.get(f"{proj.base}/records/{created['fact']}").status_code == 404
    assert proj.cs(bob, star_schema_ops()).status_code == 404

    # Bob's own project can't use Alice's record or model ids.
    tb = bob.post("/tenants", json={"name": "Globex"}).json()["id"]
    pb = bob.post(f"/tenants/{tb}/projects", json={"name": "Ops"}).json()["id"]
    mb = bob.post(f"/tenants/{tb}/projects/{pb}/models", json={"name": "M"}).json()["id"]
    foreign_ref = bob.post(f"/tenants/{tb}/projects/{pb}/changesets", json={"model_id": mb, "operations": [
        {"op": "create", "type": "attribute", "data": {"table_id": created["fact"], "name": "x", "logical_type": "text"}}]})
    assert foreign_ref.status_code == 409 and "unknown record" in foreign_ref.json()["detail"]
    foreign_model = bob.post(f"/tenants/{tb}/projects/{pb}/changesets", json={"model_id": proj.model, "operations": star_schema_ops()})
    assert foreign_model.status_code == 404
    assert bob.get(f"/tenants/{tb}/projects/{pb}/records/{created['fact']}/history").status_code == 404

    # Viewers read but cannot write or move things.
    carol = as_user("auth0|carol")
    proj.alice.put(f"/tenants/{proj.tenant}/members/{carol.id}", json={"role": "member"})
    proj.alice.put(f"{proj.base}/members/{carol.id}", json={"role": "viewer"})
    assert proj.snap(carol)["current_seq"] == 1
    assert proj.cs(carol, star_schema_ops()).status_code == 403
    assert carol.put(f"{proj.base}/models/{proj.model}/layout", json={"positions": []}).status_code == 403


def test_agent_changesets_are_attributed_to_agent_and_initiating_user(proj, db_session_factory):
    agent_id = uuid.uuid4()
    with db_session_factory() as session:
        agent = Actor(kind=ActorKind.AGENT, id=agent_id, on_behalf_of=proj.alice.id)
        scope = tenancy.resolve_project_scope(session, agent, uuid.UUID(proj.tenant), uuid.UUID(proj.project))
        result = modeling.apply_changeset(
            session, scope,
            [CreateOp(type="question", ref="q", data={"kind": "clarification", "description": "Which calendar?"})],
            reason="BR agent clarification",
        )
        session.commit()
    history = proj.alice.get(f"{proj.base}/records/{result.created['q']}/history").json()
    assert history[0]["actor_kind"] == "agent"
    assert history[0]["actor_id"] == str(agent_id)
    assert history[0]["on_behalf_of"] == str(proj.alice.id)
