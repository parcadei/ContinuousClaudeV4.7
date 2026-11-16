"""Integration tests for the M4 report surface (VAL-400, VAL-401).

These tests exercise the FastAPI app that serves aggregated benchmark
results (``GET /results``) and renders the static HTML report
(``GET /report``). They go through ``fastapi.testclient`` — no live
server required.

The tests pin to the latest on-disk summary under
``results/fastedit/*.summary.json`` so any re-benchmark run stays
compatible without test edits.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from fasteditbench.report_app import app

RESULTS_DIR = (
    Path(__file__).resolve().parents[1] / "results" / "fastedit"
)


def _latest_summary_path() -> Path:
    """Return the newest .summary.json in the fastedit results dir."""
    candidates = sorted(RESULTS_DIR.glob("*.summary.json"))
    assert candidates, f"no summary files found under {RESULTS_DIR}"
    return candidates[-1]


def _latest_summary() -> dict:
    return json.loads(_latest_summary_path().read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# VAL-400: GET /results
# ---------------------------------------------------------------------------


def test_results_default_adapter_returns_latest_summary() -> None:
    client = TestClient(app)
    response = client.get("/results")

    assert response.status_code == 200
    body = response.json()

    # Structural invariants — shape must match reporting.aggregate output.
    assert body["adapter"] == "fastedit"
    assert "run_id" in body
    assert "overall" in body
    assert "per_language" in body
    assert "tier1_pass_rate" in body["overall"]
    assert isinstance(body["overall"]["tier1_pass_rate"], float)

    # Must agree with what's actually on disk (latest summary).
    expected = _latest_summary()
    assert body["run_id"] == expected["run_id"]
    assert body["overall"]["tier1_pass_rate"] == expected["overall"]["tier1_pass_rate"]


def test_results_explicit_adapter_query_param() -> None:
    client = TestClient(app)
    response = client.get("/results", params={"adapter": "fastedit"})
    assert response.status_code == 200
    assert response.json()["adapter"] == "fastedit"


def test_results_unknown_adapter_returns_404() -> None:
    client = TestClient(app)
    response = client.get("/results", params={"adapter": "nonexistent"})
    assert response.status_code == 404
    assert "error" in response.json()


def test_results_explicit_run_id() -> None:
    client = TestClient(app)
    latest_run_id = _latest_summary()["run_id"]
    response = client.get(
        "/results", params={"adapter": "fastedit", "run_id": latest_run_id}
    )
    assert response.status_code == 200
    assert response.json()["run_id"] == latest_run_id


def test_results_unknown_run_id_returns_404() -> None:
    client = TestClient(app)
    response = client.get(
        "/results", params={"adapter": "fastedit", "run_id": "19990101-000000"}
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# VAL-401: GET /report
# ---------------------------------------------------------------------------


ALL_LANGUAGES = [
    "c",
    "cpp",
    "csharp",
    "elixir",
    "go",
    "java",
    "kotlin",
    "php",
    "python",
    "ruby",
    "rust",
    "swift",
    "typescript",
]

FINDING_IDS = [
    "FASTEDIT-001",
    "FASTEDIT-002",
    "FASTEDIT-003",
    "FASTEDIT-004",
    "FASTEDIT-005",
    "FASTEDIT-006",
    "TLDR-C-001",
    "TLDR-MULTI-001",
]


def test_report_returns_html() -> None:
    client = TestClient(app)
    response = client.get("/report")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_report_contains_headline_tier1_percentage() -> None:
    client = TestClient(app)
    response = client.get("/report")
    html = response.text

    expected = _latest_summary()
    tier1_pct = expected["overall"]["tier1_pass_rate"] * 100
    # Rendered as "87.7%" etc — one decimal place.
    needle = f"{tier1_pct:.1f}%"
    assert needle in html, f"expected {needle!r} in headline"


def test_report_contains_methodology_disclaimer() -> None:
    client = TestClient(app)
    html = client.get("/report").text
    assert "Methodology" in html
    assert "22 canonical patterns" in html
    assert "out-of-distribution" in html
    # Disclaimer now reflects that Elixir support was added upstream.
    # Must NOT still claim Elixir is unsupported.
    assert "does not support" not in html
    assert "taxonomic completeness" not in html
    # Must explicitly acknowledge the upstream fix.
    assert "All 13 languages" in html
    assert "added" in html.lower()


def test_report_contains_all_13_languages() -> None:
    client = TestClient(app)
    html = client.get("/report").text
    for lang in ALL_LANGUAGES:
        assert lang in html, f"language {lang!r} missing from /report"


def test_report_contains_all_finding_ids() -> None:
    client = TestClient(app)
    html = client.get("/report").text
    for fid in FINDING_IDS:
        assert fid in html, f"finding {fid!r} missing from /report"


def test_report_contains_distribution_sections() -> None:
    client = TestClient(app)
    html = client.get("/report").text
    # Minimality + hunk distribution sections must be named explicitly.
    assert "minimality" in html.lower()
    assert "hunk" in html.lower()


def test_report_headline_includes_total_cases_and_tokens() -> None:
    client = TestClient(app)
    html = client.get("/report").text
    expected = _latest_summary()
    assert str(expected["overall"]["n_cases"]) in html
    assert str(expected["overall"]["total_tokens_generated"]) in html


def test_report_unknown_adapter_returns_404() -> None:
    client = TestClient(app)
    response = client.get("/report", params={"adapter": "nonexistent"})
    assert response.status_code == 404
