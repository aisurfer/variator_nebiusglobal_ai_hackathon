"""Graph integration fixtures: skip without Memgraph; drop the test tenant's subgraph after each test."""
from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def memgraph_up():
    from kb.graph import memgraph as mg

    if not mg.enabled():
        pytest.skip("graph projection disabled (KB_GRAPH_ENABLED=false)")
    if not mg.available():
        pytest.skip("Memgraph not reachable (run `make infra`)")
    mg.ensure_schema(force=True)
    return True


@pytest.fixture
def gt(db, tenant, memgraph_up):
    """The fresh `tenant`, with its Memgraph subgraph removed after the test (Memgraph is shared)."""
    yield tenant
    from kb.graph.projector import reset_tenant

    reset_tenant(tenant)
