"""Synthetic demo company data.

``numeric`` builds deterministic demo cases with NumPy (no LLM). ``designer``
adds text columns with NeMo Data Designer and Nemotron on Nebius Token Factory;
it imports ``data_designer`` lazily, so this package works without the
``synthetic`` extra as long as the designer functions are not called.
"""

from variator.synthetic.designer import (
    API_KEY_ENV,
    LIGHTNING_ALIAS,
    TOKEN_FACTORY_PROVIDER,
    DesignerResult,
    TextKind,
    TokenFactoryOnlyError,
    add_text_tables,
    available_text_kinds,
    build_commentary_config,
    build_feedback_config,
    build_text_config,
    build_vendor_email_config,
    commentary_seed_rows,
    feedback_seed_rows,
    lightning_model_config,
    make_data_designer,
    run_designer,
    text_records_table,
    token_factory_provider,
    vendor_email_seed_rows,
)
from variator.synthetic.models import DemoCase, DemoTable
from variator.synthetic.numeric import (
    generate_all_demo_cases,
    generate_demo_case,
    list_demo_cases,
    write_all_demo_cases,
)
from variator.synthetic.storage import load_demo_case, write_demo_case

__all__ = [
    "API_KEY_ENV",
    "LIGHTNING_ALIAS",
    "TOKEN_FACTORY_PROVIDER",
    "DemoCase",
    "DemoTable",
    "DesignerResult",
    "TextKind",
    "TokenFactoryOnlyError",
    "add_text_tables",
    "available_text_kinds",
    "build_commentary_config",
    "build_feedback_config",
    "build_text_config",
    "build_vendor_email_config",
    "commentary_seed_rows",
    "feedback_seed_rows",
    "generate_all_demo_cases",
    "generate_demo_case",
    "lightning_model_config",
    "list_demo_cases",
    "load_demo_case",
    "make_data_designer",
    "run_designer",
    "text_records_table",
    "token_factory_provider",
    "vendor_email_seed_rows",
    "write_all_demo_cases",
    "write_demo_case",
]
