"""Built-in provider adapters. Importing this package registers them in kb.connectors.base.REGISTRY."""
from importlib import import_module

for _name in ("directory", "jira", "confluence", "gitlab", "email", "sim"):
    try:
        import_module(f"kb.connectors.{_name}")
    except ModuleNotFoundError as exc:  # an adapter not implemented yet must not break the others
        if exc.name != f"kb.connectors.{_name}":
            raise
