"""Preset texts say what a probability op does (03-REQUIREMENTS §8, item 8): "from 30% to 65%", not "+50%".

An op with ``probability`` q combines a risk probability p as 1 - (1 - p)(1 - q), so "+50%" was wrong.
The numbers use the format of the case-config chips (``scripts/mvp/export_case._op_text``).
"""

import re

from variator_it_services import delivery_model
from variator_it_services.company import company_model
from variator_it_services.scenarios import SCENARIOS

MODELS = {"company_model": company_model, "delivery_model": delivery_model}


def test_probability_ops_name_the_old_and_the_new_chance():
    checked = 0
    for template, presets in SCENARIOS.items():
        risks = {r.name: r.probability for r in MODELS[template]().risks}
        for preset in presets:
            assert not re.search(r"\(\+\d+%\)|% higher chance|more likely", preset.description), preset.name
            for op in preset.ops:
                if op.kind == "risk" and op.probability is not None:
                    p = risks[op.target]
                    after = 1 - (1 - p) * (1 - op.probability)
                    # the numbers of the model, or no numbers (QA m9: an assumed chance next to a learned one)
                    numbers = f"from {p:.0%} to {after:.0%}" in preset.description
                    assert numbers or not re.search(r"\d+%", preset.description), (preset.name, op.target)
                    checked += numbers
    assert checked >= 7
