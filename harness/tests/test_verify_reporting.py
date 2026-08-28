from __future__ import annotations

from harness.verify import CaseResult, VerificationReport, format_report


def test_report_counts_pass_and_fail():
    report = VerificationReport(
        results=[
            CaseResult(1, True),
            CaseResult(2, False, "diverge"),
            CaseResult(3, True),
        ]
    )
    assert report.passed_count == 2
    assert report.failed_count == 1
    assert not report.all_passed
    assert report.failed_case_ids == [2]


def test_all_passed_true_when_no_failures():
    report = VerificationReport(results=[CaseResult(1, True), CaseResult(2, True)])
    assert report.all_passed
    assert report.failed_case_ids == []


def test_format_report_includes_cell_id_and_failed_cases():
    report = VerificationReport(results=[CaseResult(1, False, "motivo x")])
    text = format_report(report, "e1-postgres")
    assert "e1-postgres" in text
    assert "1" in text
    assert "motivo x" in text


def test_format_report_omits_failure_detail_when_all_pass():
    report = VerificationReport(results=[CaseResult(1, True)])
    text = format_report(report, "e1-postgres")
    assert "case_ids que falharam" not in text
