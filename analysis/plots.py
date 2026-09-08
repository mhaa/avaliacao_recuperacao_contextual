"""Gráficos para o texto do TCC — fronteira de Pareto (triagem), comparação
de percentis entre células da fronteira (confirmação), latência × vazão
(rampa até o SLO) e taxa de acerto de cache (H3). Backend Agg forçado: os
containers de `tools`/`service` não têm display, e um backend interativo
travaria tentando abrir uma janela que não existe."""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

from pathlib import Path  # noqa: E402

import matplotlib.pyplot as plt  # noqa: E402


def plot_pareto_frontier(
    cells: list[dict],
    out_path: Path,
    frontier_cell_ids: set[str] | None = None,
    cost_by_cell: dict[str, float] | None = None,
) -> None:
    """Fronteira de Pareto em 2 dimensões: latência p99 × custo por milhão de
    requisições, na capacidade máxima de UMA unidade de atendimento
    (docs/DESIGN.md) — não depende mais de uma demanda `D` externa.

    Células sem custo definido (sem `S` medido, ou censuradas sem
    `lower_bound`) não são plotadas: não há onde colocá-las no eixo x, e
    inventar uma posição seria pior que omiti-las — elas aparecem em
    `cells_without_cost`."""
    fig, ax = plt.subplots()
    frontier_cell_ids = frontier_cell_ids or set()
    cost_by_cell = cost_by_cell or {}

    for c in cells:
        cost = cost_by_cell.get(c["cell_id"])
        if cost is None:
            continue
        in_frontier = c["cell_id"] in frontier_cell_ids
        ax.scatter(
            [cost],
            [c["latency_p99_ms"]],
            marker="o" if in_frontier else "x",
            s=80 if in_frontier else 40,
        )
        ax.annotate(c["cell_id"], (cost, c["latency_p99_ms"]), fontsize=8)

    ax.set_xlabel("Custo (US$ por milhão de requisições)")
    ax.set_ylabel("Latência p99 (ms)")
    ax.set_title("Fronteira de Pareto — latência × custo por milhão de requisições")
    _save(fig, out_path)


def plot_percentile_comparison(
    percentiles_by_cell: dict[str, dict[str, float]], out_path: Path
) -> None:
    """`percentiles_by_cell`: {"e1-postgres": {"p50": .., "p95": .., "p99": .., "p999": ..}, ...}
    — confirmação: comparação de percentis entre as células da fronteira."""
    labels = ["p50", "p95", "p99", "p999"]
    fig, ax = plt.subplots()
    n_cells = max(len(percentiles_by_cell), 1)
    width = 0.8 / n_cells
    for i, (cell_id, values) in enumerate(percentiles_by_cell.items()):
        offsets = [x + i * width for x in range(len(labels))]
        ax.bar(offsets, [values[label] for label in labels], width=width, label=cell_id)
    ax.set_xticks([x + width * (n_cells - 1) / 2 for x in range(len(labels))])
    ax.set_xticklabels(labels)
    ax.set_ylabel("Latência (ms)")
    ax.set_title("Comparação de percentis — células da fronteira (confirmação)")
    ax.legend()
    _save(fig, out_path)


def plot_latency_vs_throughput(
    points_by_cell: dict[str, list[tuple[float, float]]], out_path: Path
) -> None:
    """`points_by_cell`: {"e1-postgres": [(throughput_rps, latency_p99_ms), ...], ...}
    — rampa até violar o SLO (docs/DESIGN.md: p99 > 200 ms)."""
    fig, ax = plt.subplots()
    for cell_id, points in points_by_cell.items():
        ax.plot([p[0] for p in points], [p[1] for p in points], marker="o", label=cell_id)
    ax.axhline(200, linestyle="--", color="red", label="SLO (p99 = 200 ms)")
    ax.set_xlabel("Vazão (req/s)")
    ax.set_ylabel("Latência p99 (ms)")
    ax.set_title("Latência × vazão — rampa até violar o SLO")
    ax.legend()
    _save(fig, out_path)


def plot_cache_hit_rate(hit_rate_by_cache_layer: dict[str, float], out_path: Path) -> None:
    """`hit_rate_by_cache_layer`: {"none": 0.0, "candidates": .., "response": ..}
    — H3: candidatos por usuário vs. resposta completa (docs/DESIGN.md)."""
    fig, ax = plt.subplots()
    ax.bar(list(hit_rate_by_cache_layer), list(hit_rate_by_cache_layer.values()))
    ax.set_ylabel("Taxa de acerto de cache")
    ax.set_title("H3 — candidatos vs. resposta completa")
    _save(fig, out_path)


def _save(fig, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)
