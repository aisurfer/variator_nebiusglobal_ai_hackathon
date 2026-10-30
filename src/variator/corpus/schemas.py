"""JSON Schema 2020-12 exports for Plancy and corpus delivery records."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import jsonschema

from variator.corpus.models import (
    Acl,
    AclRule,
    ChainTable,
    CorpusDocument,
    CorpusManifest,
    DirectoryEntry,
    DocumentMeta,
    EntityMap,
    EvaluationCase,
    ExternalObjectIdentity,
    FactorObservation,
    FactorRelationship,
    GoldLink,
    MeetingProtocol,
    Membership,
    NorthStarReport,
    PlancyEvent,
    ProviderAdapterRecord,
    Tombstone,
)


@lru_cache(maxsize=1)
def schemas() -> dict[str, dict[str, Any]]:
    """Return a mapping of record name to its JSON Schema 2020-12 document."""
    documents = {
        "FactorCatalogEntry": {
            "type": "object",
            "additionalProperties": False,
            "required": ["factor_id", "group", "definition", "supported_units", "permitted_roles"],
            "properties": {
                "factor_id": {"type": "string"},
                "group": {"type": "string"},
                "definition": {"type": "string"},
                "supported_units": {"type": "array", "items": {"type": "string"}},
                "permitted_roles": {
                    "type": "array",
                    "items": {"enum": ["outcome", "driver", "constraint", "risk", "lever"]},
                },
            },
        },
        "CorpusDocument": CorpusDocument.model_json_schema(),
        "FactorObservation": FactorObservation.model_json_schema(),
        "Acl": Acl.model_json_schema(),
        "AclRule": AclRule.model_json_schema(),
        "Membership": Membership.model_json_schema(),
        "DocumentMeta": DocumentMeta.model_json_schema(),
        "MeetingProtocol": MeetingProtocol.model_json_schema(),
        "NorthStarReport": NorthStarReport.model_json_schema(),
        "Tombstone": Tombstone.model_json_schema(),
        "PlancyEvent": PlancyEvent.model_json_schema(mode="serialization"),
        "ChainTable": ChainTable.model_json_schema(),
        "CorpusManifest": CorpusManifest.model_json_schema(),
        "EntityMap": EntityMap.model_json_schema(),
        "ExternalObjectIdentity": ExternalObjectIdentity.model_json_schema(),
        "DirectoryEntry": DirectoryEntry.model_json_schema(),
        "ProviderAdapterRecord": ProviderAdapterRecord.model_json_schema(),
        "GoldLink": GoldLink.model_json_schema(),
        "EvaluationCase": EvaluationCase.model_json_schema(),
        "FactorRelationship": FactorRelationship.model_json_schema(),
    }
    for document in documents.values():
        document["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return documents


@lru_cache(maxsize=1)
def _validators() -> dict[str, jsonschema.protocols.Validator]:
    """Compile each delivery validator once for large production assemblies."""
    return {name: jsonschema.Draft202012Validator(schema) for name, schema in schemas().items()}


def validate_record(name: str, record: Any) -> None:
    """Validate a record dict against its declared schema; raise on failure."""
    _validators()[name].validate(record)
