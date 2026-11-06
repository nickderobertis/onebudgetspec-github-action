## Budget report

Repository: nickderobertis/onepipeline · head: 1111111111111111111111111111111111111111 · run: 1000000001 · attempt: 1

| Budget | Verdict | Current | Mean | SD | Change % | Sigma | Samples | Status |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| .github/budgets.yaml / onepipeline-macos-jobs-per-run (jobs) | within | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 20 |  |
| .github/budgets.yaml / onepipeline-macos-runs-per-change (runs) | within | 2.000 | 2.000 | 0.000 | 0.000 | 0.000 | 20 |  |
| .github/reporting/budgets.yaml / onepipeline-budget-report-command-seconds (seconds) | within | 11.400 | 10.900 | 0.800 | 4.587 | 0.625 | 20 |  |
| budgets.yaml / stacked-spikes-stage-time (seconds) | within | 34.000 | 39.800 | 2.100 | -14.573 | -2.762 | 20 |  |
| budgets.yaml / source-hold-store-operations-per-hour (operations) | within | 3.000 | 3.000 | 0.000 | 0.000 | 0.000 | 20 |  |
| budgets.yaml / store-operations-per-dispatch (operations) | within | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 20 |  |
| budgets.yaml / store-operations-per-settlement (operations) | within | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 20 |  |
| budgets.yaml / settlement-readability-latency (milliseconds) | within | 612.000 | 540.000 | 48.000 | 13.333 | 1.500 | 20 | adverse change ≥1 SD |
| budgets.yaml / stop-guard-unpublished-latency (seconds) | within | 0.142 | 0.138 | 0.009 | 2.899 | 0.444 | 20 |  |
| budgets.yaml / stop-guard-unpublished-latency-cold (seconds) | within | 0.371 | 0.356 | 0.022 | 4.213 | 0.682 | 20 |  |
| budgets.yaml / stop-guard-unpublished-latency-10x (seconds) | within | 0.684 | 0.702 | 0.031 | -2.564 | -0.581 | 20 |  |
| budgets.yaml / stop-guard-unpublished-journey-time (seconds) | within | 128.400 | 131.700 | 6.200 | -2.506 | -0.532 | 20 |  |
| budgets.yaml / host-view-time (milliseconds) | within | 62.300 | 58.900 | 4.400 | 5.772 | 0.773 | 20 |  |
| budgets.yaml / review-round-github-quota (quota-units) | within | 142.000 | 142.000 | 0.000 | 0.000 | 0.000 | 20 |  |
| budgets.yaml / check-visibility-github-quota (quota-units) | within | 198.000 | 198.000 | 0.000 | 0.000 | 0.000 | 20 |  |
| budgets.yaml / onepipeline-ci-check-time (seconds) | within | 2264.000 | 1871.000 | 196.000 | 21.005 | 2.005 | 20 | report-only; adverse change ≥1 SD |
| budgets.yaml / onepipeline-ci-job-count (jobs) | within | 5.000 | 5.000 | 0.000 | 0.000 | 0.000 | 20 | report-only |
| tests/human_items/budgets.yaml / human-items-read-seconds (seconds) | within | 0.295 | 0.290 | 0.020 | 1.724 | 0.250 | 6 | provisional |

16 budgets are left out because a pull request cannot measure them (label not-measured-on-pr).

Baseline provenance (saved observations; artifact links may expire):

Diagnostic: complete results are in artifact budget-results of https://github.com/nickderobertis/onepipeline/actions/runs/1000000001; artifacts expire under the repository's retention setting, after which that baseline reads as unavailable

### Per-head adverse flags

1111111111111111111111111111111111111111 (attempt 1): 2 adverse flag(s)
