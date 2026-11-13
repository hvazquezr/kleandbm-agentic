"""Tenant and project isolation through the HTTP API, against a real PostgreSQL."""

import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import IntegrityError

from app.domain import tenancy
from app.domain.access import (
    AccessDenied,
    Actor,
    ActorKind,
    Permission,
    ProjectRole,
)
from app.persistence.models import Base, ProjectMembership


@pytest.fixture
def world(as_user):
    """Two tenants with one project each, plus a third user who belongs to neither."""
    alice, bob, carol = as_user("auth0|alice"), as_user("auth0|bob"), as_user("auth0|carol")
    tenant_a = alice.post("/tenants", json={"name": "Acme"}).json()["id"]
    tenant_b = bob.post("/tenants", json={"name": "Globex"}).json()["id"]
    project_a = alice.post(f"/tenants/{tenant_a}/projects", json={"name": "Sales"}).json()["id"]
    project_b = bob.post(f"/tenants/{tenant_b}/projects", json={"name": "Ops"}).json()["id"]
    return dict(
        alice=alice, bob=bob, carol=carol,
        tenant_a=tenant_a, tenant_b=tenant_b, project_a=project_a, project_b=project_b,
    )


def test_requires_authentication(client, make_token):
    assert client.get("/tenants").status_code == 401
    bad = {"Authorization": f"Bearer {make_token('auth0|x', aud='https://wrong')}"}
    assert client.get("/tenants", headers=bad).status_code == 401


def test_users_see_only_their_tenants(world):
    names = [t["name"] for t in world["alice"].get("/tenants").json()]
    assert names == ["Acme"]
    assert world["carol"].get("/tenants").json() == []


def test_cannot_reach_another_tenants_resources(world):
    bob, ta, pa = world["bob"], world["tenant_a"], world["project_a"]
    assert bob.get(f"/tenants/{ta}/projects").status_code == 404
    assert bob.get(f"/tenants/{ta}/projects/{pa}").status_code == 404
    assert bob.post(f"/tenants/{ta}/projects", json={"name": "x"}).status_code == 404
    assert bob.patch(f"/tenants/{ta}/projects/{pa}", json={"name": "x"}).status_code == 404


def test_cross_tenant_id_mixing_is_rejected(world):
    # Bob's own tenant combined with Alice's project id must not resolve.
    bob = world["bob"]
    assert bob.get(f"/tenants/{world['tenant_b']}/projects/{world['project_a']}").status_code == 404


def test_member_of_both_tenants_cannot_mix_tenant_and_project(world, db_session_factory):
    # Alice joins Bob's tenant, so both tenant ids are legitimately hers. Pairing tenant B
    # with her tenant-A project must still not resolve, because a scope carrying the wrong
    # tenant would let later project-only queries cross tenants.
    alice, bob = world["alice"], world["bob"]
    tb, pa = world["tenant_b"], world["project_a"]
    assert bob.put(f"/tenants/{tb}/members/{alice.id}", json={"role": "member"}).status_code == 204
    assert alice.get(f"/tenants/{tb}/projects/{pa}").status_code == 404

    with db_session_factory() as session:
        actor = Actor(kind=ActorKind.USER, id=alice.id)
        with pytest.raises(tenancy.NotFoundInScope):
            tenancy.resolve_project_scope(session, actor, uuid.UUID(tb), uuid.UUID(pa))


def test_unknown_and_foreign_ids_look_the_same(world):
    bob, ta = world["bob"], world["tenant_a"]
    foreign = bob.get(f"/tenants/{ta}/projects/{world['project_a']}")
    unknown = bob.get(f"/tenants/{ta}/projects/{uuid.uuid4()}")
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()


def test_tenant_membership_does_not_grant_project_access(world):
    alice, carol, ta, pa = world["alice"], world["carol"], world["tenant_a"], world["project_a"]
    assert alice.put(f"/tenants/{ta}/members/{carol.id}", json={"role": "admin"}).status_code == 204
    # Even a tenant admin needs explicit project membership.
    assert carol.get(f"/tenants/{ta}/projects").json() == []
    assert carol.get(f"/tenants/{ta}/projects/{pa}").status_code == 404


def test_project_roles_are_enforced(world):
    alice, carol, ta, pa = world["alice"], world["carol"], world["tenant_a"], world["project_a"]
    alice.put(f"/tenants/{ta}/members/{carol.id}", json={"role": "member"})
    assert alice.put(
        f"/tenants/{ta}/projects/{pa}/members/{carol.id}", json={"role": "viewer"}
    ).status_code == 204

    got = carol.get(f"/tenants/{ta}/projects/{pa}")
    assert got.status_code == 200 and got.json()["role"] == "viewer"
    edit = carol.patch(f"/tenants/{ta}/projects/{pa}", json={"name": "Renamed"})
    assert edit.status_code == 403
    assert edit.json()["required_permission"] == Permission.PROJECT_EDIT
    assert carol.put(
        f"/tenants/{ta}/projects/{pa}/members/{carol.id}", json={"role": "owner"}
    ).status_code == 403

    alice.put(f"/tenants/{ta}/projects/{pa}/members/{carol.id}", json={"role": "editor"})
    assert carol.patch(f"/tenants/{ta}/projects/{pa}", json={"name": "Renamed"}).status_code == 200


def test_tenant_members_cannot_manage_membership(world):
    alice, carol, ta = world["alice"], world["carol"], world["tenant_a"]
    alice.put(f"/tenants/{ta}/members/{carol.id}", json={"role": "member"})
    assert carol.put(f"/tenants/{ta}/members/{carol.id}", json={"role": "admin"}).status_code == 403


def test_project_member_must_belong_to_tenant(world):
    alice, ta, pa = world["alice"], world["tenant_a"], world["project_a"]
    resp = alice.put(f"/tenants/{ta}/projects/{pa}/members/{world['bob'].id}", json={"role": "viewer"})
    assert resp.status_code == 409


def test_last_owner_and_last_admin_are_kept(world):
    alice, ta, pa = world["alice"], world["tenant_a"], world["project_a"]
    assert alice.put(
        f"/tenants/{ta}/projects/{pa}/members/{alice.id}", json={"role": "viewer"}
    ).status_code == 409
    assert alice.put(f"/tenants/{ta}/members/{alice.id}", json={"role": "member"}).status_code == 409


def test_schema_rejects_cross_tenant_membership(world, db_session_factory):
    # The composite foreign key blocks a membership pointing at another tenant's project,
    # even if application code were bypassed.
    with db_session_factory() as session:
        session.add(
            ProjectMembership(
                tenant_id=uuid.UUID(world["tenant_b"]),
                project_id=uuid.UUID(world["project_a"]),
                user_id=world["bob"].id,
                role=ProjectRole.OWNER,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_agent_actor_is_bounded_by_initiating_user(world, db_session_factory):
    alice, carol, ta, pa = world["alice"], world["carol"], world["tenant_a"], world["project_a"]
    alice.put(f"/tenants/{ta}/members/{carol.id}", json={"role": "member"})
    alice.put(f"/tenants/{ta}/projects/{pa}/members/{carol.id}", json={"role": "approver"})

    with db_session_factory() as session:
        agent = Actor(kind=ActorKind.AGENT, id=uuid.uuid4(), on_behalf_of=carol.id)
        scope = tenancy.resolve_project_scope(session, agent, uuid.UUID(ta), uuid.UUID(pa))
        scope.require(Permission.PROJECT_EDIT)
        with pytest.raises(AccessDenied):
            scope.require(Permission.PROJECT_APPROVE)

        orphan = Actor(kind=ActorKind.AGENT, id=uuid.uuid4())
        with pytest.raises(tenancy.NotFoundInScope):
            tenancy.resolve_project_scope(session, orphan, uuid.UUID(ta), uuid.UUID(pa))


def test_migrations_match_models(db_engine):
    with db_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
