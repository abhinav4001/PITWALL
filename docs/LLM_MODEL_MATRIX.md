# LLM Model Matrix

Checked 2026-10-21. No model is enabled until it passes the structured-output and no-arithmetic contract test.

| Role / model | Reachability | Structured output | No-arithmetic contract | Enabled |
|---|---|---|---|---|
| OmniRoute local gateway (`http://localhost:20128/v1`) | Responds (dashboard at port 20128; `/v1/models` returns HTTP 401 pending gateway credential configuration) | Not tested | Not tested | No |
| Embeddings route | Not tested via gateway; local fallback planned | N/A | N/A | No |

## Notes
- OmniRoute is running locally (`v3.8.51`). The dashboard is reachable at `http://localhost:20128`.
- In accordance with ADR-2 and ADR-5, local gateway downtime or authentication requirements are treated as a graceful degradation mode. The deterministic fast path and physics engine require zero LLM infrastructure.
