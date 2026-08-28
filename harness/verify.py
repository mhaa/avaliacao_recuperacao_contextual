"""Compara a resposta de uma célula (estratégia + storage já montados)
contra o oráculo. Ver CONTEXTO.md, "regra de ouro da implementação":
nenhuma latência é medida antes de passar 100% aqui.

Ordem importa (mesmo desempate do oráculo); uma resposta curta que bate com
o oráculo não é suspeita — 649 dos 1000 casos são assim, de propósito.
Score compara com tolerância de ponto flutuante (oracle.parquet guarda
float32; comparação exata seria um bug latente).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.contract import Request, ResponseItem
from storage.base import StorageAdapter
from strategies.base import Strategy

from .oracle import OracleCase

SCORE_TOLERANCE = 1e-4


@dataclass
class CaseResult:
    case_id: int
    passed: bool
    reason: str = ""


@dataclass
class VerificationReport:
    results: list[CaseResult] = field(default_factory=list)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    @property
    def all_passed(self) -> bool:
        return self.failed_count == 0

    @property
    def failed_case_ids(self) -> list[int]:
        return [r.case_id for r in self.results if not r.passed]


def verify_case(case: OracleCase, actual_items: list[ResponseItem]) -> CaseResult:
    actual_item_ids = [item.item_id for item in actual_items]
    if actual_item_ids != case.expected_item_ids:
        return CaseResult(
            case.case_id,
            False,
            f"item_ids esperado={case.expected_item_ids} obtido={actual_item_ids}",
        )
    for item, expected_score in zip(actual_items, case.expected_scores):
        if abs(item.score - expected_score) > SCORE_TOLERANCE:
            return CaseResult(
                case.case_id,
                False,
                f"score do item {item.item_id} diverge: esperado={expected_score} obtido={item.score}",
            )
    return CaseResult(case.case_id, True)


async def verify_cell(
    strategy: Strategy, storage: StorageAdapter, cases: list[OracleCase]
) -> VerificationReport:
    report = VerificationReport()
    for case in cases:
        req = Request(
            user_id=case.user_id, context=case.context_ids, exclude=case.exclude_ids, k=case.k
        )
        response = await strategy.retrieve(storage, req)
        report.results.append(verify_case(case, response.items))
    return report


def format_report(report: VerificationReport, cell_id: str) -> str:
    total = len(report.results)
    lines = [f"{cell_id}: {report.passed_count}/{total} casos passaram"]
    if not report.all_passed:
        lines.append(f"case_ids que falharam (até 10): {report.failed_case_ids[:10]}")
        for result in report.results:
            if not result.passed:
                lines.append(f"  case {result.case_id}: {result.reason}")
    return "\n".join(lines)
