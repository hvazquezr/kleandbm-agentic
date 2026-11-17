"""Source connections, encrypted credentials, MCP tool calls, evidence, and isolation.

Integration tests against the real Northstar MySQL database run when
KLEANDBM_DEV_NORTHSTAR_PASSWORD is available (env or backend/.env, written by
eval/northstar/load.sh); otherwise they're skipped.
"""

import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.adapters.sources import ConnectionParams, InProcessConnector, StdioMySqlConnector, ToolResult, _cap
from app.config import get_settings
from app.domain.sources import SecretBox
from app.domain.tenancy import InvalidOperation
from app.persistence.sources import SourceConnection

ROOT = Path(__file__).resolve().parents[2]
MCP_DIR = ROOT / "mcp"
FINGERPRINT = "f" * 64


def _northstar_password() -> str | None:
    if value := os.environ.get("KLEANDBM_DEV_NORTHSTAR_PASSWORD"):
        return value
    env = ROOT / "backend" / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("KLEANDBM_DEV_NORTHSTAR_PASSWORD="):
                return line.split("=", 1)[1].strip()
    return None


NORTHSTAR_PASSWORD = _northstar_password()
needs_mysql = pytest.mark.skipif(not NORTHSTAR_PASSWORD, reason="Northstar MySQL not loaded (eval/northstar/load.sh)")


# Columns the fake source reports for describe_table, so source_refs can be checked against them.
FAKE_COLUMNS = {"stores": ["store_id", "name", "channel"],
                "order_lines": ["order_line_id", "order_id", "store_id", "product_id", "quantity", "net_amount"]}


class FakeConnector:
    """Records the parameters it was opened with and returns canned results. Set read_only to
    False to simulate an account with write privileges, or fail_connect to simulate a bad password."""

    def __init__(self) -> None:
        self.opened: list[ConnectionParams] = []
        self.read_only = True
        self.fail_connect = False

    @contextmanager
    def open(self, params: ConnectionParams):
        self.opened.append(params)
        if self.fail_connect:
            raise ConnectionError("Access denied for user (using password: YES)")
        outer = self

        class S:
            def call(self, tool, arguments):
                if tool == "account_privileges":
                    problems = [] if outer.read_only else ["DELETE, INSERT, UPDATE on *.*"]
                    return ToolResult(False, {"read_only": outer.read_only, "problems": problems,
                                              "warnings": [], "grants": ["GRANT SELECT ..."]}, None)
                if tool == "list_tables":
                    return ToolResult(False, {"schema_fingerprint": FINGERPRINT, "tables": [{"table": "orders"}]}, None)
                if arguments.get("table") == "missing":
                    return ToolResult(True, None, "unknown table 'missing'")
                extra = {}
                if tool == "describe_table" and arguments.get("table") in FAKE_COLUMNS:
                    extra["columns"] = [{"name": c} for c in FAKE_COLUMNS[arguments["table"]]]
                return ToolResult(False, {"schema_fingerprint": FINGERPRINT, "tool": tool, **arguments, **extra}, None)

        yield S()


@pytest.fixture
def secrets_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(get_settings(), "secrets_key", key)
    return key


@pytest.fixture
def proj(as_user, client, secrets_key):
    connector = FakeConnector()
    client.app.state.source_connector = connector
    alice = as_user("auth0|alice")
    tenant = alice.post("/tenants", json={"name": "Northstar"}).json()["id"]
    project = alice.post(f"/tenants/{tenant}/projects", json={"name": "Discovery"}).json()["id"]

    class P:
        pass

    p = P()
    p.alice, p.tenant, p.project, p.connector = alice, tenant, project, connector
    p.base = f"/tenants/{tenant}/projects/{project}"

    def add_member(user, role):
        alice.put(f"/tenants/{tenant}/members/{user.id}", json={"role": "member"})
        alice.put(f"{p.base}/members/{user.id}", json={"role": role})

    p.add_member = add_member
    return p


CONN = {"name": "northstar", "host": "127.0.0.1", "port": 3306, "database": "northstar_ops",
        "username": "northstar_ro", "password": "s3cret-pass"}


# --- Unit -----------------------------------------------------------------------------------


def test_secret_box_round_trip_and_wrong_key():
    box = SecretBox(Fernet.generate_key().decode())
    sealed = box.seal("hunter2")
    assert sealed != "hunter2" and box.open(sealed) == "hunter2"
    with pytest.raises(InvalidOperation):
        SecretBox(Fernet.generate_key().decode()).open(sealed)
    with pytest.raises(InvalidOperation):
        SecretBox("")


def test_large_results_are_trimmed_not_dropped():
    data = {"schema_fingerprint": FINGERPRINT, "rows": [["x" * 50] for _ in range(1000)]}
    trimmed, truncated = _cap(data, 5_000)
    assert truncated and 0 < len(trimmed["rows"]) < 1000
    assert trimmed["schema_fingerprint"] == FINGERPRINT


# --- API with a fake connector ---------------------------------------------------------------


def test_connection_credentials_are_encrypted_and_never_returned(proj, db_session_factory, secrets_key):
    resp = proj.alice.post(f"{proj.base}/sources", json=CONN)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "password" not in str(body) and "s3cret-pass" not in resp.text

    with db_session_factory() as s:
        row = s.scalar(select(SourceConnection))
        assert "s3cret-pass" not in row.password_encrypted
        assert SecretBox(secrets_key).open(row.password_encrypted) == "s3cret-pass"

    # The password is only decrypted to open a session.
    proj.alice.post(f"{proj.base}/sources/{body['id']}/tools/list_tables", json={})
    assert proj.connector.opened[-1].password == "s3cret-pass"

    assert proj.alice.post(f"{proj.base}/sources", json=CONN).status_code == 409  # duplicate name


def test_host_allow_list_blocks_arbitrary_hosts(proj):
    for host in ["10.0.0.5", "metadata.google.internal", "169.254.169.254"]:
        resp = proj.alice.post(f"{proj.base}/sources", json={**CONN, "name": host, "host": host})
        assert resp.status_code == 409 and "allowed" in resp.json()["detail"]


def test_tool_calls_are_recorded_as_attributed_evidence(proj):
    conn = proj.alice.post(f"{proj.base}/sources", json=CONN).json()
    url = f"{proj.base}/sources/{conn['id']}/tools"
    ok = proj.alice.post(f"{url}/describe_table", json={"arguments": {"table": "orders"}, "purpose": "grain check"})
    assert ok.status_code == 200
    e = ok.json()
    assert e["tool"] == "describe_table" and e["arguments"] == {"table": "orders"} and not e["is_error"]
    assert e["schema_fingerprint"] == FINGERPRINT and e["purpose"] == "grain check"
    assert e["actor_kind"] == "user" and e["actor_id"] == str(proj.alice.id)

    failed = proj.alice.post(f"{url}/row_count", json={"arguments": {"table": "missing"}}).json()
    assert failed["is_error"] and "unknown table" in failed["error"]  # errors are evidence too

    assert proj.alice.post(f"{url}/drop_table", json={}).status_code == 409  # not an allowed tool

    listed = proj.alice.get(f"{proj.base}/sources/{conn['id']}/evidence").json()
    assert [x["tool"] for x in listed] == ["row_count", "describe_table"]
    assert proj.alice.get(f"{proj.base}/evidence/{e['id']}").json()["id"] == e["id"]
    source = proj.alice.get(f"{proj.base}/sources").json()[0]
    assert source["last_schema_fingerprint"] == FINGERPRINT


def test_source_permissions_by_role(proj, as_user):
    conn = proj.alice.post(f"{proj.base}/sources", json=CONN).json()
    editor, viewer = as_user("auth0|ed"), as_user("auth0|vi")
    proj.add_member(editor, "editor")
    proj.add_member(viewer, "viewer")

    # Only owners add connections (they hold credentials).
    assert editor.post(f"{proj.base}/sources", json={**CONN, "name": "x"}).status_code == 403
    # Editors investigate; viewers can read evidence but not run tools.
    assert editor.post(f"{proj.base}/sources/{conn['id']}/tools/list_tables", json={}).status_code == 200
    assert viewer.post(f"{proj.base}/sources/{conn['id']}/tools/list_tables", json={}).status_code == 403
    assert len(viewer.get(f"{proj.base}/sources/{conn['id']}/evidence").json()) == 1


def test_sources_and_evidence_are_tenant_scoped(proj, as_user):
    conn = proj.alice.post(f"{proj.base}/sources", json=CONN).json()
    evidence = proj.alice.post(f"{proj.base}/sources/{conn['id']}/tools/list_tables", json={}).json()

    bob = as_user("auth0|bob")
    assert bob.get(f"{proj.base}/sources").status_code == 404
    tb = bob.post("/tenants", json={"name": "Globex"}).json()["id"]
    pb = bob.post(f"/tenants/{tb}/projects", json={"name": "Ops"}).json()["id"]
    bob_base = f"/tenants/{tb}/projects/{pb}"
    # Alice's connection and evidence ids don't resolve in Bob's own project.
    assert bob.post(f"{bob_base}/sources/{conn['id']}/tools/list_tables", json={}).status_code == 404
    assert bob.get(f"{bob_base}/sources/{conn['id']}/evidence").status_code == 404
    assert bob.get(f"{bob_base}/evidence/{evidence['id']}").status_code == 404
    assert len(proj.connector.opened) == 2  # Alice's verify + her one call; Bob never got a session


# --- Integration with the real Northstar database --------------------------------------------


def _northstar_params() -> ConnectionParams:
    return ConnectionParams("127.0.0.1", 3306, "northstar_ops", "northstar_ro", NORTHSTAR_PASSWORD or "")


def _in_process_connector():
    sys.path.insert(0, str(MCP_DIR))
    from mysql_source.inspector import Config, Inspector
    from mysql_source.server import build_server

    return InProcessConnector(
        lambda p: build_server(Inspector(Config(p.host, p.port, p.username, p.password, p.database)))
    )


@needs_mysql
def test_mcp_tools_reveal_northstar_traps():
    with _in_process_connector().open(_northstar_params()) as session:
        tables = session.call("list_tables", {}).data
        assert {t["table"] for t in tables["tables"]} >= {"orders", "order_lines", "refunds", "store_daily_costs"}

        # Fan-out: several refunds per order line, so joining refunds to lines duplicates measures.
        fan = session.call("relationship_check", {
            "child_table": "refunds", "child_columns": ["order_line_id"],
            "parent_table": "order_lines", "parent_columns": ["order_line_id"],
        }).data
        assert fan["observed_cardinality"] == "many_to_one" and fan["parents_with_multiple_children"] > 0
        assert fan["orphan_child_rows"] == 0

        # Discrepancy 2 is discoverable: a pilot store the reports don't mention.
        stores = session.call("sample_rows", {"table": "stores", "limit": 10}).data
        assert ["HBR", "Harbor", "store", "pilot"] == stores["rows"][3][1:5]

        # Costs exist only per store per day (profit by order is unsupported as requested).
        costs = session.call("describe_table", {"table": "store_daily_costs"}).data
        assert costs["primary_key"] == ["store_id", "cost_date", "cost_type"]

        # Guest checkouts: customer_id is nullable and often null.
        profile = session.call("column_profile", {"table": "orders", "columns": ["customer_id"]}).data
        assert profile["columns"][0]["null_rate"] > 0.1

        # Identifiers outside the schema are refused before any SQL runs.
        bad = session.call("describe_table", {"table": "orders; DROP TABLE orders"})
        assert bad.is_error and "unknown table" in bad.error
        assert session.call("sample_rows", {"table": "orders", "columns": ["status` FROM x; --"]}).is_error


@needs_mysql
def test_stdio_connector_runs_the_real_mcp_server_process():
    connector = StdioMySqlConnector(MCP_DIR, 5000, 20.0, 200_000)
    with connector.open(_northstar_params()) as session:
        result = session.call("row_count", {"table": "orders"})
        assert not result.is_error and result.data["rows"] > 1000
        assert len(result.schema_fingerprint) == 64


@needs_mysql
def test_read_only_account_cannot_write():
    import pymysql

    conn = pymysql.connect(host="127.0.0.1", user="northstar_ro", password=NORTHSTAR_PASSWORD, database="northstar_ops")
    try:
        with conn.cursor() as cur, pytest.raises(pymysql.err.OperationalError) as exc:
            cur.execute("UPDATE stores SET store_name = 'x' WHERE store_id = 1")
        assert exc.value.args[0] == 1142  # command denied by grant
    finally:
        conn.close()


def test_connection_ids_dont_cross_projects_in_the_same_tenant(proj, as_user):
    # Alice owns two projects in one tenant. A connection or evidence id from project 1 must
    # not resolve through project 2, even though she can reach both projects.
    conn = proj.alice.post(f"{proj.base}/sources", json=CONN).json()
    evidence = proj.alice.post(f"{proj.base}/sources/{conn['id']}/tools/list_tables", json={}).json()
    other = proj.alice.post(f"/tenants/{proj.tenant}/projects", json={"name": "Other"}).json()["id"]
    other_base = f"/tenants/{proj.tenant}/projects/{other}"

    assert proj.alice.post(f"{other_base}/sources/{conn['id']}/tools/list_tables", json={}).status_code == 404
    assert proj.alice.get(f"{other_base}/sources/{conn['id']}/evidence").status_code == 404
    assert proj.alice.get(f"{other_base}/evidence/{evidence['id']}").status_code == 404
    assert proj.alice.get(f"{other_base}/sources").json() == []



# --- Credential safety (P4a) ------------------------------------------------------------------


def test_credentials_are_verified_before_they_are_stored(proj, db_session_factory):
    proj.connector.fail_connect = True
    resp = proj.alice.post(f"{proj.base}/sources", json=CONN)
    assert resp.status_code == 409 and "could not connect" in resp.json()["detail"]

    proj.connector.fail_connect = False
    proj.connector.read_only = False
    resp = proj.alice.post(f"{proj.base}/sources", json=CONN)
    assert resp.status_code == 409 and "isn't read-only" in resp.json()["detail"] and "INSERT" in resp.json()["detail"]

    with db_session_factory() as s:
        assert s.scalar(select(SourceConnection)) is None  # nothing stored for either attempt

    proj.connector.read_only = True
    ok = proj.alice.post(f"{proj.base}/sources", json=CONN).json()
    assert ok["privilege_check"]["read_only"] is True and ok["privilege_check"]["tables"] == 1


def test_privileges_are_rechecked_before_every_session(proj):
    conn = proj.alice.post(f"{proj.base}/sources", json=CONN).json()
    proj.connector.read_only = False  # someone granted write access after the connection was saved
    resp = proj.alice.post(f"{proj.base}/sources/{conn['id']}/tools/list_tables", json={})
    assert resp.status_code == 409 and "no longer read-only" in resp.json()["detail"]


def test_rotate_test_and_disable(proj, db_session_factory, secrets_key, as_user):
    conn = proj.alice.post(f"{proj.base}/sources", json=CONN).json()
    rotated = proj.alice.patch(f"{proj.base}/sources/{conn['id']}", json={"password": "n3w-pass"})
    assert rotated.status_code == 200
    with db_session_factory() as s:
        assert SecretBox(secrets_key).open(s.scalar(select(SourceConnection)).password_encrypted) == "n3w-pass"

    # A rotation that fails verification leaves the old credentials in place.
    proj.connector.fail_connect = True
    assert proj.alice.patch(f"{proj.base}/sources/{conn['id']}", json={"password": "wrong"}).status_code == 409
    proj.connector.fail_connect = False
    with db_session_factory() as s:
        assert SecretBox(secrets_key).open(s.scalar(select(SourceConnection)).password_encrypted) == "n3w-pass"

    editor = as_user("auth0|ed")
    proj.add_member(editor, "editor")
    assert editor.post(f"{proj.base}/sources/{conn['id']}/test").status_code == 200
    assert editor.patch(f"{proj.base}/sources/{conn['id']}", json={"password": "x"}).status_code == 403
    assert editor.post(f"{proj.base}/sources/{conn['id']}/disable").status_code == 403

    disabled = proj.alice.post(f"{proj.base}/sources/{conn['id']}/disable").json()
    assert disabled["disabled_at"] is not None
    assert proj.alice.post(f"{proj.base}/sources/{conn['id']}/tools/list_tables", json={}).status_code == 409


def test_models_link_to_sources(proj, as_user):
    conn = proj.alice.post(f"{proj.base}/sources", json=CONN).json()
    model = proj.alice.post(f"{proj.base}/models", json={"name": "Sales"}).json()["id"]
    url = f"{proj.base}/models/{model}/sources"
    assert proj.alice.put(f"{url}/{conn['id']}").status_code == 204
    assert proj.alice.put(f"{url}/{conn['id']}").status_code == 204  # idempotent
    assert [c["name"] for c in proj.alice.get(url).json()] == ["northstar"]
    viewer = as_user("auth0|vi")
    proj.add_member(viewer, "viewer")
    assert viewer.put(f"{url}/{conn['id']}").status_code == 403
    assert proj.alice.delete(f"{url}/{conn['id']}").status_code == 204
    assert proj.alice.get(url).json() == []


def test_grant_assessment():
    sys.path.insert(0, str(MCP_DIR))
    from mysql_source.inspector import assess_grants

    ro = ["GRANT USAGE ON *.* TO `ro`@`%`", "GRANT SELECT, SHOW VIEW ON `northstar_ops`.* TO `ro`@`%`"]
    assert assess_grants(ro, "northstar_ops")["read_only"] is True
    for grants in (["GRANT SELECT, INSERT ON `db`.* TO `u`@`%`"],
                   ["GRANT ALL PRIVILEGES ON `db`.* TO `u`@`%`"],
                   ["GRANT SELECT ON `db`.* TO `u`@`%` WITH GRANT OPTION"],
                   ["GRANT BACKUP_ADMIN,ROLE_ADMIN ON *.* TO `u`@`%`"],
                   ["GRANT `writer`@`%` TO `u`@`%`"]):
        assert assess_grants(grants, "db")["read_only"] is False, grants
    assert assess_grants(["GRANT SELECT ON *.* TO `u`@`%`"], "db")["warnings"]


@needs_mysql
def test_real_read_only_account_passes_the_privilege_check():
    with _in_process_connector().open(_northstar_params()) as session:
        check = session.call("account_privileges", {}).data
        assert check["read_only"] is True and not check["problems"]


@needs_mysql
@pytest.mark.parametrize("password,port,expected", [
    ("definitely-wrong", 3306, "Access denied for user 'northstar_ro'"),
    ("", 3399, "Can't connect to MySQL server"),
])
def test_connection_failures_read_plainly(password, port, expected):
    """The real MySQL error reaches the user, not an exception group or the SDK's wrapper text."""
    from app.domain.sources import verify_params

    with pytest.raises(InvalidOperation) as err:
        verify_params(_in_process_connector(), ConnectionParams("127.0.0.1", port, "northstar_ops", "northstar_ro", password))
    message = str(err.value)
    assert message.startswith("could not connect to MySQL:") and expected in message, message
    assert "ExceptionGroup" not in message and "Error executing tool" not in message


def test_exception_groups_are_unwrapped():
    from app.domain.sources import _first_leaf

    leaf = ConnectionError("Access denied")
    assert _first_leaf(ExceptionGroup("tg", [ExceptionGroup("inner", [leaf])])) is leaf
    assert _first_leaf(leaf) is leaf


def test_credentials_are_redacted_from_chat(proj, client):
    from app.adapters.llm import ScriptedChatModel

    model = ScriptedChatModel([{"text": "Please add the source on the Sources page."}])
    client.app.state.chat_model_factory = lambda *_: model
    conv = proj.alice.post(f"{proj.base}/conversations", json={}).json()
    resp = proj.alice.post(f"{proj.base}/conversations/{conv['id']}/messages",
                           json={"text": "use mysql://northstar_ro:S3cret-Pass@127.0.0.1/northstar_ops"})
    first = [l for l in resp.text.splitlines() if l.startswith("data: ")][0]
    assert '"redacted_credentials": true' in first
    stored = proj.alice.get(f"{proj.base}/conversations/{conv['id']}/messages").json()[0]["text"]
    assert "S3cret-Pass" not in stored and "[redacted credential]" in stored
    assert "S3cret-Pass" not in str(model.calls)  # never reached the model
