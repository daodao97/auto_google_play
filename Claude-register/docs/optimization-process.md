# Optimization Process

This process applies to performance, reliability, and operational-safety changes.
Correctness, recoverability, and data protection take priority over throughput.

## Safety Boundaries

- Do not optimize for bypassing CAPTCHA, anti-automation, rate limits, access controls,
  or account restrictions.
- Do not use real accounts or external services unless the user explicitly authorizes a
  single controlled manual run for the current task.
- Do not print or record account identifiers, passwords, tokens, sessions, proxy data,
  challenge material, headers, request bodies, response bodies, or raw exceptions.
- Preserve `register` and `session` mode boundaries, request contracts, result buckets,
  and persistence semantics unless a reviewed change contract explicitly says otherwise.

## Baseline

Record a baseline before changing behavior. Each record must use only these desensitized
fields:

| Field | Rule |
| --- | --- |
| `run_id` | Random identifier, never derived from an account. |
| `build_id` | Commit ID or non-sensitive working-tree marker. |
| `config_hash` | Hash of a secret-free configuration summary. |
| `source` | `offline`, `local`, or `authorized_manual`. |
| `mode` | Existing supported mode only. |
| `stage` | Existing stage name. |
| `outcome` | `success`, `partial`, `failed`, `unknown`, or `persistence_error`. |
| `elapsed_ms` | Whole-run or stage duration. |
| `attempts` | Existing attempt count. |
| `retryable` | Existing retry decision. |
| `error_class` | Stable non-sensitive category only. |

Always report sample size before median or p95. Segment by stage and outcome. Never
combine `partial`, `unknown`, or `persistence_error` with `success`.

## Experiment Contract

Every behavior-changing experiment must state:

```text
Scope and files:
Current behavior:
Proposed behavior:
Protected invariants:
Desensitized baseline:
Acceptance metric:
Stop condition:
Rollback point:
```

Test one hypothesis and change one variable at a time. Keep observability, default-value
changes, retry changes, UI changes, and refactors in separate experiments.

## Execution

1. Confirm the worktree state and preserve all local runtime data.
2. Add an offline test that fails for the behavior under review.
3. Make the smallest reversible implementation change.
4. Run syntax checks, focused tests, and the full offline suite.
5. Compare only against the recorded baseline build and configuration hash.
6. Let the user decide whether to authorize a controlled manual run.
7. Record the decision as adopted, rejected, or not adopted.

## Stop And Roll Back

Stop immediately when any of these occurs:

- Failure rate increases or a new error class appears.
- Data integrity, result classification, or recovery behavior degrades.
- A protected invariant changes unexpectedly.
- The candidate has no measurable improvement at the recorded sample size.
- Sensitive data appears in logs, UI snapshots, reports, or errors.

Rollback to the named commit or clean working-tree marker. Do not discard local config,
results, journals, logs, or other operator-owned runtime data during rollback.

## Delivery Report

Each delivery must include changed files, protected areas intentionally untouched,
verification commands and results, before/after metrics when available, known risks,
and the rollback point.
