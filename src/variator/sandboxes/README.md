# variator.sandboxes

Run LLM-written simulation code in Token Factory Sandboxes (beta, access per project). Optional: fall back to `simulation.simulate`.

| Need | Use |
|---|---|
| Client, image | `SandboxesClient`, `ensure_python_image` |
| Run one program | `run_python` |
| Write, run, check, fix loop (Ultra) | `generate_and_run` |
| Offline tests | `sandboxes.testing.FakeSandboxes` |

Output modes, errors, safety, assumptions: `docs/knowledge/sandboxes.md`.
