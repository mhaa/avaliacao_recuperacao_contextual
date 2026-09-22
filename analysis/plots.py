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


# --- Rampa de estresse com foco no banco ------------------------------------
# docs/DESIGN.md, "Experimento complementar". As três figuras compartilham o
# MESMO eixo x (carga ofertada) de propósito: é o que permite ler as três
# alinhadas — "na taxa em que a vazão dobra, a CPU do banco está em X%".

# Marcador distinto para degraus com offered_load_ok=False. A ressalva de que
# ali os percentis são só dos sobreviventes precisa estar na figura que vai
# para o texto, não apenas numa nota de rodapé (docs/DESIGN.md, "Vazão
# ofertada verificada, não presumida").
_DEGRADED_MARKER = "x"
_HEALTHY_MARKER = "o"


def _legend_if_labelled(ax) -> None:
    """Uma execução abortada cedo produz eixos sem nenhuma série. Chamar
    `legend()` aí emite UserWarning a cada figura — ruído que esconderia
    avisos reais no log de uma campanha."""
    if ax.get_legend_handles_labels()[0]:
        ax.legend(fontsize="small")


def _split_by_phase(steps: list[dict]) -> tuple[list[dict], list[dict]]:
    ascent = [s for s in steps if s["phase"] in ("coarse_up", "fine_up", "plateau")]
    descent = [s for s in steps if s["phase"] == "fine_down"]
    return ascent, descent


def _scatter_degraded(ax, steps: list[dict], y_key: str, color) -> None:
    degraded = [s for s in steps if s.get("offered_load_ok") is False and s.get(y_key) is not None]
    if degraded:
        ax.scatter(
            [s["rate"] for s in degraded],
            [s[y_key] for s in degraded],
            marker=_DEGRADED_MARKER,
            color=color,
            zorder=3,
        )


def plot_offered_vs_sustained(steps: list[dict], out_path: Path, title: str = "") -> None:
    """Carga ofertada × vazão sustentada, com a diagonal y=x como ideal.

    O joelho é onde a curva descola da diagonal. Subida e descida em estilos
    distintos: a separação entre as duas É a histerese, e uma curva só não
    distinguiria "recuperou" de "se perdeu".
    """
    fig, ax = plt.subplots()
    ascent, descent = _split_by_phase(steps)

    for label, group, style, color in (
        ("subida", ascent, "-", "tab:blue"),
        ("descida", descent, "--", "tab:orange"),
    ):
        points = [(s["rate"], s["throughput_rps"]) for s in group if s.get("throughput_rps")]
        if not points:
            continue
        ax.plot(
            [p[0] for p in points],
            [p[1] for p in points],
            style,
            marker=_HEALTHY_MARKER,
            color=color,
            label=label,
        )
        _scatter_degraded(ax, group, "throughput_rps", color)

    offered = [s["rate"] for s in steps]
    if offered:
        limit = max(offered)
        ax.plot([0, limit], [0, limit], ":", color="gray", label="ideal (y = x)")

    ax.set_xlabel("Carga ofertada (req/s)")
    ax.set_ylabel("Vazão sustentada (req/s)")
    ax.set_title(title or "Carga ofertada × vazão sustentada")
    _legend_if_labelled(ax)
    _save(fig, out_path)


def plot_percentiles_by_step(steps: list[dict], out_path: Path, title: str = "") -> None:
    """p50/p95/p99 por degrau, com a linha de SLO.

    As três curvas juntas mostram a assimetria que docs/DESIGN.md usa para
    proibir médias: sob sobrecarga o p50 fica quase parado enquanto o p99
    dispara, e a separação entre elas é o dado.
    """
    fig, ax = plt.subplots()
    ascent, _ = _split_by_phase(steps)

    for key, label, color in (
        ("latency_ms_p50", "p50", "tab:green"),
        ("latency_ms_p95", "p95", "tab:blue"),
        ("latency_ms_p99", "p99", "tab:red"),
    ):
        points = [(s["rate"], s[key]) for s in ascent if s.get(key) is not None]
        if not points:
            continue
        ax.plot(
            [p[0] for p in points],
            [p[1] for p in points],
            marker=_HEALTHY_MARKER,
            color=color,
            label=label,
        )
        _scatter_degraded(ax, ascent, key, color)

    ax.axhline(200, linestyle="--", color="black", label="SLO (p99 = 200 ms)")
    # Escala log: o p99 varia de ~3 ms a milhares após o joelho, e em escala
    # linear as três curvas colapsariam na base do gráfico antes dele.
    ax.set_yscale("log")
    ax.set_xlabel("Carga ofertada (req/s)")
    ax.set_ylabel("Latência (ms, escala log)")
    ax.set_title(title or "Percentis por patamar da rampa")
    ax.legend()
    _save(fig, out_path)


def plot_resources_by_step(
    cpu_by_component: dict[str, list[tuple[float, float]]],
    memory_by_component: dict[str, list[tuple[float, float]]],
    out_path: Path,
    title: str = "",
) -> None:
    """CPU e memória das três VMs contra a carga ofertada, em duas faixas.

    Recebe as séries JÁ alinhadas por taxa (o join por intervalo vive em
    analysis/ramp_resources.py:align_samples_to_steps, testável à parte) —
    desenhar e alinhar na mesma função tornaria o alinhamento inverificável.

    A curva "database (máx. por núcleo)" é o ponto do experimento nas células
    Valkey: com o agregado em ~12,5% e o máximo por núcleo em ~100%, as duas
    juntas provam a saturação que classify_bottleneck não consegue enxergar.
    """
    fig, (ax_cpu, ax_mem) = plt.subplots(2, 1, sharex=True, figsize=(8, 8))

    for component, points in cpu_by_component.items():
        if points:
            ax_cpu.plot([p[0] for p in points], [p[1] for p in points], marker=".", label=component)
    # 90%: o teto que analysis/resources.py:classify_bottleneck usa para
    # decidir qual recurso satura primeiro.
    ax_cpu.axhline(90, linestyle="--", color="black", label="teto de saturação (90%)")
    # 60%: portão de validade do gerador (load/saturation.py). Se a curva do
    # gerador encostar, a figura já mostra que a execução é inválida.
    ax_cpu.axhline(60, linestyle=":", color="red", label="portão do gerador (60%)")
    ax_cpu.set_ylabel("CPU (%)")
    ax_cpu.set_title(title or "Recursos por patamar da rampa")
    ax_cpu.legend(fontsize="small")

    for component, points in memory_by_component.items():
        if points:
            ax_mem.plot([p[0] for p in points], [p[1] for p in points], marker=".", label=component)
    ax_mem.set_xlabel("Carga ofertada (req/s)")
    ax_mem.set_ylabel("Memória (MB)")
    _legend_if_labelled(ax_mem)

    _save(fig, out_path)


def _save(fig, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)
