# Conformance — SPED §17 mapping

Each row maps a single bullet from `SPED.md` §17.1–§17.7 to the test
covering it in `tests/conformance/`. Run the full suite with:

```
uv run pytest -m conformance
```

All tests are `@pytest.mark.conformance`-marked; the marker is
registered in `pyproject.toml`. The suite is fast (in-process; no
network, no real subprocesses except the bounded POSIX-only SIGINT
smoke). Status legend: `OK` — implemented and asserted; `DEFERRED` —
explicitly deferred for this iteration with rationale below.

## §17.1 Workflow and Config Parsing

| Bullet | Test | Status |
|---|---|---|
| explicit runtime path is used when provided | `test_17_1_workflow_config.py::test_workflow_path_explicit_runtime_path_wins` | OK |
| cwd default is `WORKFLOW.md` when no explicit runtime path is provided | `test_17_1_workflow_config.py::test_workflow_path_default_is_workflow_md_in_cwd` | OK |
| workflow file changes are detected and trigger re-read/re-apply without restart | `test_17_1_workflow_config.py::test_workflow_change_triggers_reload` | OK |
| invalid workflow reload keeps last known good effective configuration and emits an operator-visible error | `test_17_1_workflow_config.py::test_invalid_reload_keeps_last_known_good` | OK |
| missing `WORKFLOW.md` returns typed error | `test_17_1_workflow_config.py::test_missing_workflow_returns_typed_error` | OK |
| invalid YAML front matter returns typed error | `test_17_1_workflow_config.py::test_invalid_yaml_front_matter_returns_typed_error` | OK |
| front matter non-map returns typed error | `test_17_1_workflow_config.py::test_front_matter_non_map_returns_typed_error` | OK |
| config defaults apply when OPTIONAL values are missing | `test_17_1_workflow_config.py::test_config_defaults_apply_when_optional_missing` | OK |
| `tracker.kind` validation enforces currently supported kind (`linear`) | `test_17_1_workflow_config.py::test_tracker_kind_validation_enforces_linear` | OK |
| `tracker.api_key` works (including `$VAR` indirection) | `test_17_1_workflow_config.py::test_tracker_api_key_works_including_var_indirection` | OK |
| `$VAR` resolution works for tracker API key and path values | `test_17_1_workflow_config.py::test_var_resolution_works_for_path_values` | OK |
| `~` path expansion works | `test_17_1_workflow_config.py::test_tilde_path_expansion_works` | OK |
| `codex.command` is preserved as a shell command string | `test_17_1_workflow_config.py::test_codex_command_preserved_as_shell_command_string` | OK |
| Per-state concurrency override map normalizes state names and ignores invalid values | `test_17_1_workflow_config.py::test_per_state_concurrency_normalises_and_drops_invalid` | OK |
| Prompt template renders `issue` and `attempt` | `test_17_1_workflow_config.py::test_prompt_renders_issue_and_attempt` | OK |
| Prompt rendering fails on unknown variables (strict mode) | `test_17_1_workflow_config.py::test_prompt_render_fails_on_unknown_variables` | OK |

## §17.2 Workspace Manager and Safety

| Bullet | Test | Status |
|---|---|---|
| Deterministic workspace path per issue identifier | `test_17_2_workspace.py::test_deterministic_workspace_path_per_identifier` | OK |
| Missing workspace directory is created | `test_17_2_workspace.py::test_missing_workspace_directory_is_created` | OK |
| Existing workspace directory is reused | `test_17_2_workspace.py::test_existing_workspace_directory_is_reused` | OK |
| Existing non-directory path at workspace location is handled safely | `test_17_2_workspace.py::test_existing_non_directory_at_path_raises` | OK |
| OPTIONAL workspace population/synchronization errors are surfaced | `test_17_2_workspace.py::test_after_create_failure_surfaced` | OK |
| `after_create` hook runs only on new workspace creation | `test_17_2_workspace.py::test_after_create_runs_only_on_new_creation` | OK |
| `before_run` hook runs before each attempt and failure/timeouts abort the current attempt | `test_17_2_workspace.py::test_before_run_failure_aborts_attempt` | OK |
| `after_run` hook runs after each attempt and failure/timeouts are logged and ignored | `test_17_2_workspace.py::test_after_run_failure_logged_and_ignored` | OK |
| `before_remove` hook runs on cleanup and failures/timeouts are ignored | `test_17_2_workspace.py::test_before_remove_failure_ignored` | OK |
| Workspace path sanitization and root containment invariants are enforced before agent launch | `test_17_2_workspace.py::test_path_sanitization_and_root_containment_enforced` | OK |
| Agent launch uses the per-issue workspace path as cwd and rejects out-of-root paths | `test_17_2_workspace.py::test_agent_launch_rejects_out_of_root_paths` | OK |

## §17.3 Issue Tracker Client

| Bullet | Test | Status |
|---|---|---|
| Candidate issue fetch uses active states and project slug | `test_17_3_tracker.py::test_candidate_fetch_uses_active_states_and_project_slug` | OK |
| Linear query uses the specified project filter field (`slugId`) | `test_17_3_tracker.py::test_candidates_query_uses_slug_id_filter` | OK |
| Empty `fetch_issues_by_states([])` returns empty without API call | `test_17_3_tracker.py::test_empty_fetch_issues_by_states_no_api_call` | OK |
| Pagination preserves order across multiple pages | `test_17_3_tracker.py::test_pagination_preserves_order` | OK |
| Blockers are normalized from inverse relations of type `blocks` | `test_17_3_tracker.py::test_blockers_normalized_from_inverse_relations_blocks` | OK |
| Labels are normalized to lowercase | `test_17_3_tracker.py::test_labels_normalized_to_lowercase` | OK |
| Issue state refresh by ID returns minimal normalized issues | `test_17_3_tracker.py::test_state_refresh_returns_minimal_normalized_issues` | OK |
| Issue state refresh query uses GraphQL ID typing (`[ID!]`) | `test_17_3_tracker.py::test_state_refresh_query_uses_id_bang_array` | OK |
| Error mapping for request errors | `test_17_3_tracker.py::test_error_mapping_request_error` | OK |
| Error mapping for non-200 | `test_17_3_tracker.py::test_error_mapping_non_200_status` | OK |
| Error mapping for GraphQL errors | `test_17_3_tracker.py::test_error_mapping_graphql_errors_in_body` | OK |
| Error mapping for malformed payloads | `test_17_3_tracker.py::test_error_mapping_malformed_payload` | OK |

## §17.4 Orchestrator Dispatch, Reconciliation, and Retry

| Bullet | Test | Status |
|---|---|---|
| Dispatch sort order is priority then oldest creation time | `test_17_4_orchestrator.py::test_dispatch_sort_priority_then_oldest` | OK |
| `Todo` issue with non-terminal blockers is not eligible | `test_17_4_orchestrator.py::test_todo_with_non_terminal_blocker_not_eligible` | OK |
| `Todo` issue with terminal blockers is eligible | `test_17_4_orchestrator.py::test_todo_with_terminal_blocker_is_eligible` | OK |
| Active-state issue refresh updates running entry state | `test_17_4_orchestrator.py::test_active_state_refresh_updates_running_entry` | OK |
| Non-active state stops running agent without workspace cleanup | `test_17_4_orchestrator.py::test_non_active_state_terminates_without_cleanup` | OK |
| Terminal state stops running agent and cleans workspace | `test_17_4_orchestrator.py::test_terminal_state_terminates_with_cleanup` | OK |
| Reconciliation with no running issues is a no-op | `test_17_4_orchestrator.py::test_reconciliation_with_no_running_is_noop` | OK |
| Normal worker exit schedules a short continuation retry (attempt 1) | `test_17_4_orchestrator.py::test_normal_exit_schedules_continuation_attempt_one` | OK |
| Abnormal worker exit increments retries with 10s-based exponential backoff | `test_17_4_orchestrator.py::test_abnormal_exit_uses_10s_exponential_backoff` | OK |
| Retry backoff cap uses configured `agent.max_retry_backoff_ms` | `test_17_4_orchestrator.py::test_retry_backoff_uses_max_cap` | OK |
| Retry queue entries include attempt, due time, identifier, and error | `test_17_4_orchestrator.py::test_retry_entry_carries_attempt_due_id_error` | OK |
| Stall detection kills stalled sessions and schedules retry | `test_17_4_orchestrator.py::test_stall_detection_returns_stalled_ids` | OK |
| Slot exhaustion requeues retries with explicit error reason | `test_17_4_orchestrator.py::test_slot_exhaustion_reschedule_with_explicit_error` | OK |
| If a snapshot API is implemented, it returns running rows, retry rows, token totals, and rate limits | `test_17_4_orchestrator.py::test_snapshot_includes_running_retry_tokens_rate_limits` | OK |
| If a snapshot API is implemented, timeout/unavailable cases are surfaced | `test_17_4_orchestrator.py::test_snapshot_timeout_unavailable_modes_are_documented` | OK |

## §17.5 Coding-Agent App-Server Client

| Bullet | Test | Status |
|---|---|---|
| Launch command uses workspace cwd and invokes `bash -lc <codex.command>` | `test_17_5_codex.py::test_launch_uses_workspace_cwd_via_bash_lc` | OK |
| Session startup follows the targeted Codex app-server protocol | `test_17_5_codex.py::test_session_startup_follows_three_step_handshake` | OK |
| Client identity/capability payloads are valid when required | `test_17_5_codex.py::test_client_capability_payloads_valid_when_required` | OK |
| Policy-related startup payloads use documented approval/sandbox settings | `test_17_5_codex.py::test_policy_payloads_use_documented_settings` | OK |
| Thread and turn identities are extracted and used to emit `session_started` | `test_17_5_codex.py::test_thread_and_turn_ids_extracted_and_composed_to_session_id` | OK |
| Request/response read timeout is enforced | `test_17_5_codex.py::test_read_timeout_enforced` | OK |
| Turn timeout is enforced | `test_17_5_codex.py::test_turn_timeout_enforced` | OK |
| Transport framing is handled correctly | `test_17_5_codex.py::test_transport_framing_is_handled` | OK |
| Diagnostic stderr handling is kept separate from the protocol stream | `test_17_5_codex.py::test_stderr_isolated_from_protocol_stream` | OK |
| Command/file-change approvals are handled per documented policy | `test_17_5_codex.py::test_approvals_handled_per_documented_policy` | OK |
| Unsupported dynamic tool calls are rejected without stalling the session | `test_17_5_codex.py::test_unsupported_dynamic_tool_calls_rejected_without_stalling` | OK |
| User input requests are handled per documented policy and do not stall | `test_17_5_codex.py::test_user_input_requests_handled_per_documented_policy` | OK |
| Usage and rate-limit telemetry exposed by the protocol is extracted | `test_17_5_codex.py::test_usage_telemetry_extracted_from_protocol_payloads` | OK |
| Approval, user-input-required, usage, and rate-limit signals are interpreted per protocol | `test_17_5_codex.py::test_signals_interpreted_per_protocol` | OK |
| Client-side tools advertised when implemented | `test_17_5_codex.py::test_client_side_tools_advertised_when_implemented` | OK |
| `linear_graphql` — tool advertised to the session | `test_17_5_codex.py::test_linear_graphql_advertised_to_session` | OK |
| `linear_graphql` — valid query/variables execute against configured Linear auth | `test_17_5_codex.py::test_linear_graphql_valid_inputs_executed` | OK |
| `linear_graphql` — top-level GraphQL errors produce `success=false` while preserving the GraphQL body | `test_17_5_codex.py::test_linear_graphql_top_level_errors_preserve_body` | OK |
| `linear_graphql` — invalid arguments / missing auth / transport failures return structured failure payloads | `test_17_5_codex.py::test_linear_graphql_invalid_args_returns_failure` | OK |
| `linear_graphql` — unsupported tool names still fail without stalling the session | `test_17_5_codex.py::test_unsupported_tool_name_does_not_stall` | OK |

## §17.6 Observability

| Bullet | Test | Status |
|---|---|---|
| Validation failures are operator-visible | `test_17_6_observability.py::test_validation_failures_operator_visible` | OK |
| Structured logging includes issue/session context fields | `test_17_6_observability.py::test_structured_logging_carries_issue_session_context` | OK |
| Logging sink failures do not crash orchestration | `test_17_6_observability.py::test_logging_sink_failure_does_not_crash_orchestration` | OK |
| Token/rate-limit aggregation remains correct across repeated agent updates | `test_17_6_observability.py::test_token_rate_limit_aggregation_correct_across_updates` | OK |
| Status surface driven from orchestrator state without affecting correctness | `test_17_6_observability.py::test_status_surface_driven_from_state_no_correctness_impact` | OK |
| Humanized event summaries don't change orchestrator behavior | `test_17_6_observability.py::test_humanized_summaries_dont_change_orchestrator_behavior` | DEFERRED — humanizers not implemented; orchestrator decisions use raw event names |

## §17.7 CLI and Host Lifecycle

| Bullet | Test | Status |
|---|---|---|
| CLI accepts a positional workflow path argument | `test_17_7_cli.py::test_cli_accepts_positional_workflow_path` | OK |
| CLI uses `./WORKFLOW.md` when no workflow path argument is provided | `test_17_7_cli.py::test_cli_uses_workflow_md_default` | OK |
| CLI errors on nonexistent explicit workflow path | `test_17_7_cli.py::test_cli_errors_on_nonexistent_explicit_path` | OK |
| CLI errors on missing default `./WORKFLOW.md` | `test_17_7_cli.py::test_cli_errors_on_missing_default` | OK |
| CLI surfaces startup failure cleanly | `test_17_7_cli.py::test_cli_surfaces_startup_failure_cleanly` | OK |
| CLI exits with success when application starts and shuts down normally | `test_17_7_cli.py::test_cli_exits_zero_when_application_shuts_down_normally` | OK |
| CLI exits nonzero when startup fails or the host process exits abnormally | `test_17_7_cli.py::test_cli_exits_nonzero_on_startup_or_abnormal_failure` | OK |
| End-to-end SIGINT bounded exit (POSIX) | `test_17_7_cli.py::test_subprocess_sigint_bounded_exit` | OK |

## SPED §18 Acceptance Criteria

§18 acceptance criteria are mostly aggregations of §17 bullets. Direct
mappings reference §17 tests; gaps for spec-mandated default values
and integration-level checks are covered by
`tests/conformance/test_18_acceptance.py`.

### §18.1 REQUIRED for Conformance

| Bullet | Test | Status |
|---|---|---|
| Workflow path selection supports explicit runtime path and cwd default | `test_17_1_workflow_config.py::test_workflow_path_explicit_runtime_path_wins` + `::test_workflow_path_default_is_workflow_md_in_cwd` | OK |
| `WORKFLOW.md` loader with YAML front matter + prompt body split | `test_17_1_workflow_config.py::test_invalid_yaml_front_matter_returns_typed_error` + `::test_front_matter_non_map_returns_typed_error` | OK |
| Typed config layer with defaults and `$` resolution | `test_17_1_workflow_config.py::test_config_defaults_apply_when_optional_missing` + `::test_var_resolution_works_for_path_values` | OK |
| Dynamic `WORKFLOW.md` watch/reload/re-apply for config and prompt | `test_17_1_workflow_config.py::test_workflow_change_triggers_reload` + `::test_invalid_reload_keeps_last_known_good` | OK |
| Polling orchestrator with single-authority mutable state | `test_18_acceptance.py::test_polling_orchestrator_single_authority_state` | OK |
| Issue tracker client with candidate fetch + state refresh + terminal fetch | `test_17_3_tracker.py::test_candidate_fetch_uses_active_states_and_project_slug` + `::test_state_refresh_returns_minimal_normalized_issues` + `::test_empty_fetch_issues_by_states_no_api_call` | OK |
| Workspace manager with sanitized per-issue workspaces | `test_17_2_workspace.py::test_deterministic_workspace_path_per_identifier` + `::test_path_sanitization_and_root_containment_enforced` | OK |
| Workspace lifecycle hooks (`after_create`, `before_run`, `after_run`, `before_remove`) | `test_17_2_workspace.py::test_after_create_*` + `::test_before_run_failure_aborts_attempt` + `::test_after_run_failure_logged_and_ignored` + `::test_before_remove_failure_ignored` | OK |
| Hook timeout config (`hooks.timeout_ms`, default `60000`) | `test_18_acceptance.py::test_hooks_timeout_default_60000_ms` | OK |
| Coding-agent app-server subprocess client with JSON line protocol | `test_18_acceptance.py::test_codex_client_uses_json_line_protocol` | OK |
| Codex launch command config (`codex.command`, default `codex app-server`) | `test_18_acceptance.py::test_codex_command_default_is_codex_app_server` | OK |
| Strict prompt rendering with `issue` and `attempt` variables | `test_17_1_workflow_config.py::test_prompt_renders_issue_and_attempt` + `::test_prompt_render_fails_on_unknown_variables` | OK |
| Exponential retry queue with continuation retries after normal exit | `test_17_4_orchestrator.py::test_normal_exit_schedules_continuation_attempt_one` + `::test_abnormal_exit_uses_10s_exponential_backoff` | OK |
| Configurable retry backoff cap (`agent.max_retry_backoff_ms`, default 5m) | `test_18_acceptance.py::test_max_retry_backoff_default_5_minutes` + `test_17_4_orchestrator.py::test_retry_backoff_uses_max_cap` | OK |
| Reconciliation that stops runs on terminal/non-active tracker states | `test_17_4_orchestrator.py::test_terminal_state_terminates_with_cleanup` + `::test_non_active_state_terminates_without_cleanup` | OK |
| Workspace cleanup for terminal issues (startup sweep + active transition) | `test_18_acceptance.py::test_startup_terminal_workspace_cleanup_sweep` + `test_17_4_orchestrator.py::test_terminal_state_terminates_with_cleanup` | OK |
| Structured logs with `issue_id`, `issue_identifier`, and `session_id` | `test_17_6_observability.py::test_structured_logging_carries_issue_session_context` | OK |
| Operator-visible observability (structured logs; OPTIONAL snapshot/status surface) | `test_17_6_observability.py::test_validation_failures_operator_visible` + `test_17_4_orchestrator.py::test_snapshot_includes_running_retry_tokens_rate_limits` | OK |

### §18.2 RECOMMENDED Extensions (shipped)

| Bullet | Test | Status |
|---|---|---|
| HTTP server extension honors CLI `--port` over `server.port` | `test_18_acceptance.py::test_http_extension_cli_port_propagates_to_start_service` | OK |
| HTTP server extension uses a safe default bind host | `test_18_acceptance.py::test_http_extension_loopback_default_bind` | OK |
| HTTP server extension exposes baseline endpoints from §13.7 | `test_18_acceptance.py::test_http_extension_baseline_endpoints_registered` | OK |
| HTTP server extension exposes baseline error semantics from §13.7 | `test_18_acceptance.py::test_http_extension_error_envelope_semantics` (+ end-to-end coverage in `tests/http/test_error_envelope.py`) | OK |
| `linear_graphql` exposes raw Linear GraphQL access through the app-server session using configured Symphony auth | `test_18_acceptance.py::test_linear_graphql_uses_configured_symphony_auth` (+ `test_17_5_codex.py::test_linear_graphql_*`) | OK |

### §18.2 RECOMMENDED Extensions (NOT shipped — TODO per spec)

These are explicitly TODO items in the spec; not implemented in this
iteration. Operators wanting them should track the upstream SPED.

| Bullet | Status |
|---|---|
| Persist retry queue and session metadata across process restarts | DEFERRED — in-memory only; restart resets retry queue (tracker state still drives recovery on next poll tick) |
| Make observability settings configurable in workflow front matter | DEFERRED — logging is `configure_logging`-fixed today; no front-matter knob |
| First-class tracker write APIs (comments/state transitions) in the orchestrator | DEFERRED — writes go through the agent via the `linear_graphql` tool |
| Pluggable issue tracker adapters beyond Linear | DEFERRED — only `tracker.kind=linear` is supported |

## Implementation-deferred fields

These are returned with safe defaults from the HTTP detail endpoint
(`GET /api/v1/{identifier}`) per Task 43:

- `tracked` — empty dict `{}`. Reserved for tracker-side debug fields
  (e.g. workspace path, last sync time). Populated when M11/M12 wires
  the corresponding metadata pipeline.
- `logs.codex_session_logs` — empty list `[]`. Codex session logs are
  not persisted to disk in this iteration; the field is reserved for
  the per-session log capture that lands alongside log-rotation work.

## Skipped tests (sandbox-related)

A handful of integration tests skip in sandboxes that deny `bind(127.0.0.1, 0)`
or filesystem-watch events. They are NOT under the `conformance` marker and
do not affect the conformance suite outcome:

- `tests/http/test_app.py` — six real-TCP tests gated on
  `_can_bind_loopback()`.
- `tests/test_cli_startup.py::test_start_service_real_port_starts_uvicorn`
  — same gate.
- `tests/workflow/test_watcher.py::<integration>` — gated on real
  watchfiles event delivery.

These exercise the same code paths as in-process ASGI / injected-awatch
tests; the conformance assertions stand without them.
