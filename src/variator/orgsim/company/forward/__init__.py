"""Day level (WP19): the company after T, day by day, from the learned habits; each example is a KB branch.

``state.StateAtT`` (history + actor artifacts) -> ``params.build_params`` (learned -> processes, source tags)
-> ``forks.Case.fork`` (dated levers of the case) -> ``sim.ForwardRun`` (the day loop; a week-tree path
forces its key events) -> ``deliver.write_branch`` (corpus delivery of a tenant) and ``day.example``
(series and event lane of the case page). ``pipeline.branches`` runs it all; CLI
``python -m variator.orgsim.company.cli branches``.
"""
