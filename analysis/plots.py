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
    demand_rps: float | None = None,
    units_by_cell: dict[str, int] | None = None,
    cost_by_cell: dict[str, float] | None = None,
) -> None:
    """Fronteira de Pareto em 2 dimensões: latência p99 × custo mensal.

    A vazão de saturação NÃO é um eixo — ela está internalizada no custo, via
    o número de unidades de atendimento `n(D)` (docs/DESIGN.md, "Custo como
    função da demanda"). Por isso o gráfico é sempre relativo a uma demanda:
    `demand_rps` vai no título, e `units_by_cell`/`cost_by_cell` chegam prontos
    de `report["demand_levels"]`, que já os calculou naquela demanda.

    Células sem custo definido naquela demanda (sem `S` medido) não são
    plotadas: não há onde colocá-las no eixo x, e inventar uma posição seria
    pior que omiti-las — elas aparecem em `cells_without_cost`."""
    fig, ax = plt.subplots()
    frontier_cell_ids = frontier_cell_ids or set()
    units_by_cell = units_by_cell or {}
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
        units = units_by_cell.get(c["cell_id"])
        label = c["cell_id"] + (f"\nn={units}" if units else "")
        ax.annotate(label, (cost, c["latency_p99_ms"]), fontsize=8)

    ax.set_xlabel("Custo total (US$/mês)")
    ax.set_ylabel("Latência p99 (ms)")
    title = "Fronteira de Pareto — latência × custo"
    if demand_rps is not None:
        title += f" (D = {demand_rps:.0f} req/s)"
    ax.set_title(title)
    _save(fig, out_path)


def plot_cost_vs_demand(cells: list[dict], crossovers: dict | None, out_path: Path) -> None:
    """Curva em degraus de `C(D)` por célula, com os cruzamentos de fronteira
    marcados — a figura que o texto promete ao falar nos "pontos em que a
    configuração de menor custo total se altera".

    `C(D) = n(D) · custo_por_unidade` é função escada, com degraus nos
    múltiplos de `S`; `cost_curve` (analysis/pareto.py) já entrega os patamares
    prontos em `cells[i]["cost_curve"]`.

    Eixo x linear, não logarítmico: o primeiro patamar começa em D = 0, que não
    tem lugar numa escala log."""
    fig, ax = plt.subplots()

    for c in cells:
        curve = c.get("cost_curve") or []
        if not curve:
            continue
        xs: list[float] = []
        ys: list[float] = []
        for step in curve:
            xs.extend([step["demand_from_rps"], step["demand_to_rps"]])
            ys.extend([step["cost_usd_month"], step["cost_usd_month"]])
        ax.plot(xs, ys, label=c["cell_id"], linewidth=1.2)

    for change in (crossovers or {}).get("frontier", []):
        ax.axvline(change["demand_rps"], linestyle="--", color="grey", linewidth=0.8)

    ax.set_xlabel("Demanda D (req/s)")
    ax.set_ylabel("Custo total (US$/mês)")
    ax.set_title("Custo × demanda — degraus nos múltiplos da vazão de saturação")
    ax.legend(fontsize=7)
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
