"""Actor cards (plan C2-C3, PRD S6): cited text profiles over the learned habits, and their KB pages.

Code builds every number, interval, source tag and citation from the WP10 artifact set
(``variator.actors``); a headless Nemotron writer only words the 3-5 sentence profile, and a
checker rejects any text that says more than its facts. Steps: ``build`` -> ``write`` ->
``artifacts`` and ``export`` (``python -m variator.actors.cards.cli``).
"""

SCHEMA = "variator.actors.cards/1"
KIND = "actor-cards"
