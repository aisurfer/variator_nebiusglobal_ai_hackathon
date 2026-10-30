# variator.cloud

Nebius Serverless AI: jobs (deep simulation on GPU) and endpoints (app hosting).

| Need | Use |
|---|---|
| REST client | `ServerlessClient` (env `NEBIUS_IAM_TOKEN`, `NEBIUS_CLOUD_PROJECT_ID`, `NEBIUS_SUBNET_ID`) |
| Deep simulation job | `submit_simulation_job` -> `wait_for_result` |
| Cost estimate | `estimate_job_cost`, `estimate_endpoint_cost` |
| Logs, token | `cli_job_logs`, `cli_access_token` |

Rules, costs, deploy notes: `docs/knowledge/cloud.md`. Commands: `deploy/README.md`.
