"""Search tests run against the real OpenSearch and the real multilingual embedding model.

`stenant` is the shared `tenant` fixture plus cleanup of the tenant's documents in the shared indices.
"""
from __future__ import annotations

import os

import pytest

from kb.acl import resolve_principal, system_principal

SEARCH_CONSUMERS = ["search", "descriptors"]


def _os_ok() -> bool:
    try:
        from kb.search.opensearch import client

        return bool(client().ping())
    except Exception:
        return False


@pytest.fixture
def stenant(db, tenant):
    if not _os_ok():
        pytest.skip("OpenSearch not reachable (run `make infra`)")
    yield tenant
    if not os.environ.get("KB_KEEP_TEST_TENANTS"):
        from kb.worker import framework

        framework.reset(db, tenant, consumers=SEARCH_CONSUMERS)


@pytest.fixture
def corpus(db, stenant):
    from tests.integration.search.corpus import build

    build(db, stenant)
    return stenant


@pytest.fixture
def admin(corpus):
    return system_principal(corpus)


@pytest.fixture
def jordan(db, corpus):
    return resolve_principal(db, corpus, "user:jordan.baird")
