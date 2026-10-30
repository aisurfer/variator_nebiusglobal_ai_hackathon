# variator.containers

Run tools in containers and get compact metrics.

| Need | Use |
|---|---|
| Local Docker | `DockerCLI` (`run`, `build`, `pull`, `deploy`, `redeploy`, `status`, `logs`) |
| Nebius job | `NebiusJobRunner` (bucket-staged); choose with `VARIATOR_RUNNER` (`runner_from_env`) |
| Scans | `RECIPES` + `run_recipe(runner, recipe, source)`; groups `GROUPS` (default, sca, secrets, duplication, architecture); `dependency_check` only by name |
| Own images | `images/*.Dockerfile` (jscpd 4, dependency-cruiser, detect-secrets), built on first use (`ensure_image`) |
| Findings | `findings.write_scan` -> `findings.json` (DefectDojo generic); `findings.DefectDojo.push_scan` |
| MCP | `container_status`, `code_scan` (-> `scan` handle), `findings_export` (env `DEFECTDOJO_URL`, `DEFECTDOJO_TOKEN`) |

Safe defaults: no network, `/src` read-only, only `/out` writable, CPU and memory limits, timeout.
