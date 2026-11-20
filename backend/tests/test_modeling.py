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


def test_acceptance_needs_approval_and_lapses_on_change(proj, as_user, client, make_token):
    carol = as_user("auth0|carol")
    proj.alice.put(f"/tenants/{proj.tenant}/members/{carol.id}", json={"role": "member"})
    proj.alice.put(f"{proj.base}/members/{carol.id}", json={"role": "editor"})
    proj.cs(proj.alice, star_schema_ops())
    surl = f"{proj.base}/models/{proj.model}/state"

    assert proj.alice.get(surl).json()["effective_state"] == "draft"
    # Set by hand, Ready for Review skips the design checks, so it needs a note saying why.
    silent = carol.post(surl, json={"state": "ready_for_review", "note": "  "})
    assert silent.status_code == 409 and "note" in silent.json()["detail"]
    assert carol.post(surl, json={"state": "ready_for_review", "note": "Reviewed the star schema"}).status_code == 200
    denied = carol.post(surl, json={"state": "accepted"})
    assert denied.status_code == 403 and denied.json()["required_permission"] == "project:approve"

    # Alice's profile arrives in her token's namespaced claims (an Auth0 Action adds them).
    profile = {"https://kleandbm.ai/name": "Alice Analyst", "https://kleandbm.ai/email": "alice@example.com"}
    assert client.get("/me", headers={"Authorization": f"Bearer {make_token('auth0|alice', **profile)}"}).status_code == 200

    accepted = proj.alice.post(surl, json={"state": "accepted", "note": "Approved for build"}).json()
    event = accepted.pop("last_event")
    assert accepted == {"recorded_state": "accepted", "recorded_at_seq": 1,
                        "effective_state": "accepted", "changed_since_state": False}
    assert (event["state"], event["seq"], event["actor_kind"], event["by_name"], event["note"]) == (
        "accepted", 1, "user", "Alice Analyst", "Approved for build")

    # Who did what is kept for every transition, newest first. Carol's token has no profile.
    history = proj.alice.get(f"{surl}/history").json()
    assert [(e["state"], e["by_name"]) for e in history] == [("accepted", "Alice Analyst"), ("ready_for_review", None)]
    # A later token without the profile claims doesn't blank the stored name.
    assert proj.alice.get(surl).json()["last_event"]["by_name"] == "Alice Analyst"

    # A register entry linked to the model counts too: here a definition its fact table uses.
    fact = next(t for t in proj.snap()["tables"] if t["data"]["name"] == "fact_sales")
    proj.cs(carol, [{"op": "create", "type": "definition", "ref": "d", "data": {"term": "Customer", "provenance": "inferred"}},
                    {"op": "link", "from_id": fact["id"], "to_id": "$d", "link_type": "uses_definition"}], model_id=None)
    lapsed = proj.alice.get(surl).json()
    assert lapsed["recorded_state"] == "accepted" and lapsed["recorded_at_seq"] == 1
    assert lapsed["effective_state"] == "draft" and lapsed["changed_since_state"] is True


def test_only_register_entries_linked_to_the_model_reset_it(proj):
    """Registers are shared by the project's models; a change that concerns only another model
    (or no model yet) leaves an accepted model accepted."""
    created = proj.cs(proj.alice, star_schema_ops()).json()["created"]
    regs = proj.cs(proj.alice, [
        {"op": "create", "type": "requirement", "ref": "r", "data": {"statement": "Sales by customer", "origin_kind": "user_statement"}},
        {"op": "create", "type": "definition", "ref": "d", "data": {"term": "Sales", "provenance": "user_stated",
                                                                     "confirmation_status": "confirmed"}},
        {"op": "link", "from_id": "$r", "to_id": created["fact"], "link_type": "maps_to"},
        {"op": "link", "from_id": "$r", "to_id": "$d", "link_type": "uses_definition"},
    ], model_id=None).json()["created"]
    other = proj.alice.post(f"{proj.base}/models", json={"name": "Inventory"}).json()["id"]
    other_table = proj.cs(proj.alice, [{"op": "create", "type": "table", "ref": "t", "data": {
        "name": "fact_stock", "kind": "fact"}}], model_id=other).json()["created"]["t"]
    surl = f"{proj.base}/models/{proj.model}/state"

    def write(ops, model_id=None):
        r = proj.cs(proj.alice, ops, model_id=model_id)
        assert r.status_code == 201, r.text
        return r

    def accept():
        assert proj.alice.post(surl, json={"state": "accepted"}).status_code == 200
        return proj.alice.get(surl).json()

    def state():
        return proj.alice.get(surl).json()["effective_state"]

    def seq_of(rid):
        return proj.alice.get(f"{proj.base}/records/{rid}").json()["seq"]

    accept()
    # Not linked to this model: a new requirement, the other model's own entries and links,
    # and the other model's element using this model's definition.
    write([
        {"op": "create", "type": "requirement", "ref": "x", "data": {"statement": "Stock by day", "origin_kind": "user_statement"}},
        {"op": "link", "from_id": "$x", "to_id": other_table, "link_type": "maps_to"},
        {"op": "create", "type": "question", "ref": "q", "data": {"kind": "clarification", "description": "Which stock?", "is_blocking": False}},
        {"op": "link", "from_id": "$q", "to_id": "$x", "link_type": "concerns"},
        {"op": "link", "from_id": other_table, "to_id": regs["d"], "link_type": "uses_definition"},
    ], model_id=None)
    write([{"op": "update", "id": other_table, "expected_seq": seq_of(other_table),
                          "data": {"grain": "One row per product per day"}}], model_id=other)
    assert state() == "accepted"

    # Linked: the mapped requirement, the definition it uses, a question about either.
    write([{"op": "update", "id": regs["r"], "expected_seq": seq_of(regs["r"]),
                          "data": {"statement": "Sales by customer and month"}}], model_id=None)
    assert state() == "draft"
    accept()
    write([{"op": "update", "id": regs["d"], "expected_seq": seq_of(regs["d"]),
                          "data": {"definition": "Gross sales before discounts"}}], model_id=None)
    assert state() == "draft"
    accept()
    write([
        {"op": "create", "type": "question", "ref": "q", "data": {"kind": "clarification", "description": "Net or gross?", "is_blocking": False}},
        {"op": "link", "from_id": "$q", "to_id": regs["d"], "link_type": "concerns"},
    ], model_id=None)
    assert state() == "draft"
    accept()
    # Unmapping the requirement changes what the model answers, so it counts too.
    mapping = next(l for l in proj.snap()["links"] if l["from_id"] == regs["r"] and l["link_type"] == "maps_to")
    assert proj.cs(proj.alice, [{"op": "unlink", "link_id": mapping["id"]}], model_id=None).status_code == 201
    assert state() == "draft"


def test_acceptance_needs_dependent_definitions_confirmed_or_excepted(proj):
    """Ready for Review doesn't need confirmed meanings; Accepted does, or a person names each
    unconfirmed definition as an exception with a note (spec §7)."""
    created = proj.cs(proj.alice, star_schema_ops()).json()["created"]
    regs = proj.cs(proj.alice, [
        {"op": "create", "type": "requirement", "ref": "r", "data": {"statement": "Sales by customer", "origin_kind": "user_statement"}},
        {"op": "create", "type": "definition", "ref": "sales", "data": {"term": "Sales", "provenance": "documented"}},
        {"op": "create", "type": "definition", "ref": "net", "data": {"term": "Net sales", "provenance": "inferred"}},
        {"op": "create", "type": "definition", "ref": "cust", "data": {"term": "Customer", "provenance": "user_stated",
                                                                        "confirmation_status": "confirmed"}},
        {"op": "create", "type": "definition", "ref": "other", "data": {"term": "Stock", "provenance": "inferred"}},
        {"op": "link", "from_id": "$r", "to_id": created["fact"], "link_type": "maps_to"},
        {"op": "link", "from_id": "$r", "to_id": "$sales", "link_type": "uses_definition"},
        {"op": "link", "from_id": "$sales", "to_id": "$net", "link_type": "conflicts_with"},
        {"op": "link", "from_id": created["dim"], "to_id": "$cust", "link_type": "uses_definition"},
    ], model_id=None).json()["created"]
    surl = f"{proj.base}/models/{proj.model}/state"

    deps = proj.alice.get(f"{proj.base}/models/{proj.model}/definition-dependencies").json()
    by_term = {d["term"]: d for d in deps}
    # The unrelated Stock definition isn't a dependency.
    assert set(by_term) == {"Sales", "Net sales", "Customer"}
    assert by_term["Sales"]["reasons"] == [f"used by requirement {proj.alice.get(f'{proj.base}/records/{regs['r']}').json()['code']}"]
    assert by_term["Net sales"]["reasons"][0].startswith("conflicts with")
    assert by_term["Customer"]["reasons"] == ["used by dim_customer"]

    assert proj.alice.post(surl, json={"state": "ready_for_review", "note": "ok"}).status_code == 200
    refused = proj.alice.post(surl, json={"state": "accepted"})
    assert refused.status_code == 409
    assert "Sales" in refused.json()["detail"] and "Net sales" in refused.json()["detail"]
    assert "Customer" not in refused.json()["detail"]
    # Exceptions must cover every open one, name only dependencies, and come with a note.
    partial = proj.alice.post(surl, json={"state": "accepted", "note": "ok", "exceptions": [regs["sales"]]})
    assert partial.status_code == 409 and "Net sales" in partial.json()["detail"]
    stranger = proj.alice.post(surl, json={"state": "accepted", "note": "ok",
                                           "exceptions": [regs["sales"], regs["net"], regs["other"]]})
    assert stranger.status_code == 409 and "exceptions must be" in stranger.json()["detail"]
    silent = proj.alice.post(surl, json={"state": "accepted", "exceptions": [regs["sales"], regs["net"]]})
    assert silent.status_code == 409 and "note" in silent.json()["detail"]
    # Exceptions only mean something when accepting.
    assert proj.alice.post(surl, json={"state": "ready_for_review", "note": "ok", "exceptions": [regs["sales"]]}).status_code == 409
    assert proj.alice.get(surl).json()["effective_state"] == "ready_for_review"

    accepted = proj.alice.post(surl, json={"state": "accepted", "note": "Finance confirms gross next sprint",
                                           "exceptions": [regs["sales"], regs["net"]]})
    assert accepted.status_code == 200, accepted.text
    event = accepted.json()["last_event"]
    assert {e["term"] for e in event["exceptions"]} == {"Sales", "Net sales"}
    assert proj.alice.get(f"{surl}/history").json()[0]["exceptions"] == event["exceptions"]

    # Rejecting the conflicting meaning settles it; confirming Sales then needs no exceptions.
    def seq_of(rid):
        return proj.alice.get(f"{proj.base}/records/{rid}").json()["seq"]
    assert proj.cs(proj.alice, [
        {"op": "update", "id": regs["net"], "expected_seq": seq_of(regs["net"]), "data": {"confirmation_status": "rejected"}},
        {"op": "update", "id": regs["sales"], "expected_seq": seq_of(regs["sales"]), "data": {"confirmation_status": "confirmed"}},
    ], model_id=None).status_code == 201
    assert proj.alice.get(surl).json()["effective_state"] == "draft"
    assert proj.alice.post(surl, json={"state": "accepted"}).status_code == 200

    # A rejected definition the model still uses can't be excepted.
    assert proj.cs(proj.alice, [{"op": "update", "id": regs["sales"], "expected_seq": seq_of(regs["sales"]),
                                 "data": {"confirmation_status": "rejected"}}], model_id=None).status_code == 201
    used_rejected = proj.alice.post(surl, json={"state": "accepted", "note": "x", "exceptions": [regs["sales"]]})
    assert used_rejected.status_code == 409 and "rejected definitions" in used_rejected.json()["detail"]


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
    # The lifecycle history (who accepted) is scoped like everything else, including mixed ids.
    assert bob.get(f"{proj.base}/models/{proj.model}/state/history").status_code == 404
    assert bob.get(f"/tenants/{tb}/projects/{pb}/models/{proj.model}/state/history").status_code == 404
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


def test_confirming_meanings_needs_approval_rights_and_decisions_need_edit(proj, as_user):
    """Business meanings (requirements, definitions) are confirmed or rejected only by people with
    approval rights; editors may confirm or reject design decisions. The UI hiding the buttons
    isn't the control: the server refuses."""
    created = proj.cs(proj.alice, [
        {"op": "create", "type": "definition", "ref": "d", "data": {"term": "Net sales", "kind": "metric", "provenance": "user_stated"}},
        {"op": "create", "type": "decision", "ref": "x", "data": {"kind": "decision", "statement": "Order-line grain"}},
    ]).json()["created"]
    erin = as_user("auth0|erin")
    proj.alice.put(f"/tenants/{proj.tenant}/members/{erin.id}", json={"role": "member"})
    proj.alice.put(f"{proj.base}/members/{erin.id}", json={"role": "editor"})
    seq = lambda rid: next(r["seq"] for k in ("definitions", "decisions") for r in proj.snap()[k] if r["id"] == rid)  # noqa: E731

    confirm_def = lambda user, status: proj.cs(user, [  # noqa: E731
        {"op": "update", "id": created["d"], "expected_seq": seq(created["d"]), "data": {"confirmation_status": status}}])
    assert confirm_def(erin, "confirmed").status_code == 403
    assert proj.cs(erin, [{"op": "create", "type": "definition", "data": {
        "term": "Gross sales", "kind": "metric", "provenance": "user_stated", "confirmation_status": "confirmed"}}]).status_code == 403
    assert confirm_def(proj.alice, "confirmed").status_code == 201  # the owner holds approval rights
    assert confirm_def(erin, "unconfirmed").status_code == 403      # nor can an editor undo it
    # Other edits by an editor are unaffected.
    assert proj.cs(erin, [{"op": "update", "id": created["d"], "expected_seq": seq(created["d"]),
                           "data": {"aliases": ["NS"]}}]).status_code == 201

    decide = lambda status: proj.cs(erin, [  # noqa: E731
        {"op": "update", "id": created["x"], "expected_seq": seq(created["x"]), "data": {"status": status}}])
    assert decide("rejected").status_code == 201 and decide("confirmed").status_code == 201
    decision = next(d for d in proj.snap()["decisions"] if d["id"] == created["x"])
    assert decision["data"]["status"] == "confirmed"
