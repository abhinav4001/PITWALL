# M0 Verification Notes

Checked 2026-10-21. Findings are provisional where official documentation or live responses did not expose the required detail; do not hardcode unverified values.

## Data sources

| Check | Finding | Status / consequence |
|---|---|---|
| OpenF1 historical coverage | Official docs say historical data is available from 2023 onward without authentication. Historical 2026 coverage is listed. | Verified at docs level; precise timestamp completeness for every historical 2026 endpoint/session still needs a sample audit. |
| OpenF1 live access | Official docs state real-time data requires a paid subscription. | Important discrepancy with the spec's assumed free live mode; validate account/access before implementing live ingestion. |
| OpenF1 sampling | Docs: car data/location about 3.7 Hz; intervals about every 4 seconds during races; weather every minute. | Treat as approximate endpoint sampling, not a guaranteed delivery SLA. |
| OpenF1 pagination/rate limits | Docs reviewed do not specify pagination contract or rate-limit policy. Filtering and CSV output are documented. | Unverified; use conservative requests and response-size handling; do not hardcode a claimed limit. |
| OpenF1 2026 sessions | `sessions?year=2026&session_type=Race` returned 31 records, dated 2026-03-08 through 2026-12-06; includes cancellation metadata. | Endpoint coverage observed. Includes race sessions beyond the 23 Grand Prix rounds, e.g. sprint session records may affect counts depending on endpoint semantics. |
| FastF1 2026 support | Official docs describe current/historical F1 data but do not state a 2026 cutoff. | ⚠ Needs an actual `get_session(...).load()` smoke test when implementing M1. |
| FastF1 data delay | Official docs confirm timing data availability but don't state a post-session delay. | ⚠ No fixed delay verified. Plan around post-session publication; measure empirically. |
| Jolpica 2026 schedule | `2026.json` returned all 23 rounds (`total=23`, page limit 30, offset 0). | Verified live response. |
| Jolpica 2026 standings | 2026 driver standings endpoint returned 23 drivers, through round 17. | Verified live response; standings remain season-in-progress. |
| Jolpica pagination | Example responses expose `limit`, `offset`, and `total`. | Verified on schedule/standings responses; still handle pagination generically. |
| Pit-lane limits | FIA event-doc search URL used was invalid (404); circuit-specific 2026 limits not verified. | ⚠ Do not hardcode a 60/80 km/h value globally. Read the relevant event notes per circuit. |
| 2026 tyre mapping | Not yet verified from official Pirelli/FIA event allocations. | ⚠ Keep compound mapping per event/session; do not assume a universal C1–C5-to-S/M/H mapping. |
| Fuel cap/burn | FIA listed 2026 Section C Technical Issue 21 (2026-10-19), but available page excerpt did not expose relevant provisions. | ⚠ Read applicable regulation text before parameterizing fuel mass/flow. Spec's 1.5 kg/lap and 0.03 s/kg are starting heuristics only, not verified constants. |

## LLM gateway (local OmniRoute)

| Check | Finding | Status / consequence |
|---|---|---|
| `GET http://localhost:20128/v1/models` | HTTP 401 Unauthorized | Gateway process responds, but model listing requires authentication. No model should be marked enabled based on this check. |
| `GET http://localhost:20128/v1/embeddings` | HTTP 401 Unauthorized | Cannot determine embeddings support without valid gateway auth. Endpoint may also require POST. |
| Model contracts | Not tested; no authorized model listing or completion run. | No model is approved/enabled. Run per-model contract tests after credentials/configuration are supplied; never put credentials in docs. |

## Sources

- [OpenF1 documentation](https://openf1.org/docs)
- [OpenF1 2026 race-session endpoint](https://api.openf1.org/v1/sessions?year=2026&session_type=Race)
- [Jolpica 2026 schedule](https://api.jolpi.ca/ergast/f1/2026.json)
- [Jolpica 2026 driver standings](https://api.jolpi.ca/ergast/f1/2026/driverstandings.json)
- [FastF1 documentation](https://docs.fastf1.dev/)
- [FIA 2026 Section C technical regulations, Issue 21](https://www.fia.com/system/files/documents/fia_2026_f1_regulations_-_section_c_technical_-_iss_21_-_2026-10-19.pdf)
- [FIA 2026 Section B sporting regulations, Issue 09](https://www.fia.com/system/files/documents/fia_2026_f1_regulations_-_section_b_sporting_-_iss_09_-_2026-10-01.pdf)

## M0 remaining verification

- [ ] Test OpenF1 historical endpoints on representative 2026 sessions for timestamped replay completeness and pagination behavior.
- [ ] Confirm whether live OpenF1 access is available under the project's intended account/subscription.
- [ ] Run FastF1 2026 session-load smoke test; record observed availability delay.
- [ ] Verify current Jolpica endpoint behavior for results/pit stops and sprint-vs-GP round numbering.
- [ ] Retrieve FIA event notes for target circuits and record pit-lane speed limits individually.
- [ ] Verify 2026 compound allocations from official event-specific Pirelli/FIA materials.
- [ ] Inspect 2026 technical regulations for fuel constraints; derive model inputs from actual 2026 data rather than transferring 2023–25 values.
- [ ] Obtain authorized OmniRoute credentials; test `/v1/models`, a minimal chat completion per intended model, and `/v1/embeddings` via POST if supported.
