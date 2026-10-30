# variator.llm

Nemotron on Nebius Token Factory.

| Need | Use |
|---|---|
| Client | `TokenFactoryClient` (`chat`, `structured`, `run_tools`); env `NEBIUS_API_KEY` |
| Models and prices | `ModelRegistry`, `LIGHTNING`, `SUPER`, `ULTRA`, `SUPER_VL` (override ids with `VARIATOR_MODEL_<KEY>`) |
| Cost | `UsageLedger` (`listeners` feed `telemetry`) |
| Offline tests | `testing.FakeTokenFactory` |

Facts and assumptions: `docs/knowledge/reasoning.md`, `docs/research/nebius-nvidia-tech-research.md`.
