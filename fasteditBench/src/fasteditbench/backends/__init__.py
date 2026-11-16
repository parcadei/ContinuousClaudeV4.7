"""FastEdit-backed adapters + health gate for the benchmark harness (M3).

Public surface:

- :func:`fasteditbench.backends.fastedit_health.check_fastedit_health`
- :func:`fasteditbench.backends.fastedit_health.fail_fast_if_unhealthy`
- :class:`fasteditbench.backends.fastedit_adapter.FastEditAdapter`

These modules intentionally import FastEdit lazily so that the rest of
the harness (NullAdapter, tiers, diff_quality) can be exercised in
environments where FastEdit / MLX are not installed.
"""
