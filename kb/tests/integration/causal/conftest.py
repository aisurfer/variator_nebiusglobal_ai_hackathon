from __future__ import annotations

import pytest

from kb.acl import resolve_principal
from tests.integration.causal import story as S


@pytest.fixture
def story(db, tenant) -> S.Story:
    return S.build(db, tenant)


@pytest.fixture
def admin(db, tenant):
    return resolve_principal(db, tenant, "user:admin")


@pytest.fixture
def jordan(db, tenant):
    """FlowPilot product manager: all-staff, product, flowpilot — no grp:exec (cannot see the Product council)."""
    return resolve_principal(db, tenant, "user:jordan.baird")


@pytest.fixture
def priya(db, tenant):
    """Director of Product: grp:exec + product."""
    return resolve_principal(db, tenant, "user:priya.nand")
