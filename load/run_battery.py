"""Bateria de execuções de load/scenarios.js — 5 repetições por célula, em
ordem aleatorizada (CONTEXTO.md, "Protocolo de medição": "5 repetições por
célula, em ordem aleatorizada"; ver o plano em
implementacao-md-com-base-nos-staged-snowflake.md, Etapa 7). A seed usada
para embaralhar fica logada no manifesto de cada execução, para
reprodutibilidade — nunca correr as células na ordem em que os arquivos
cells/*.yaml aparecem no disco, o que enviesaria efeitos de ordem (cache do
SO ainda quente, throttling térmico acumulado) sempre para a mesma célula.

`cells/*.yaml` (exceto `_defaults.yaml`) já É a lista de células viáveis —
as 2 combinações arquiteturalmente inviáveis (E-4/Scylla, E-3/OpenSearch)
nunca ganharam arquivo (ver core/registry.py,
tests/acceptance/test_infeasible_cells_fail_at_startup.py). Não há outra
checagem de viabilidade a duplicar aqui.

Uso local (SMOKE apenas — CONTEXTO.md proíbe medir latência localmente):
    docker compose run --rm --entrypoint python tools load/run_battery.py \\
        --cells e1-postgres --target-url http://service:8000/v1/recommendations \\
        --repetitions 1 --rate 10 --smoke

Uso real (nuvem — infra/, Etapa 9): cada célula roda contra sua própria VM
de serviço; --targets aponta um JSON {cell_id: target_url} produzido a
partir dos outputs `service_internal_ip` de infra/envs/experiment.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CELLS_DIR = Path("cells")
SCENARIOS_SCRIPT = Path("load/scenarios.js")
RESULTS_DIR = Path("results")


def list_viable_cell_ids(cells_dir: Path = CELLS_DIR) -> list[str]:
    return sorted(p.stem for p in cells_dir.glob("*.yaml") if p.stem != "_defaults")


def shuffled_cell_order(cell_ids: list[str], seed: int) -> list[str]:
    order = list(cell_ids)
    random.Random(seed).shuffle(order)
    return order


def build_run_plan(cell_ids: list[str], repetitions: int) -> list[tuple[str, int]]:
    """(cell_id, repetition_index) na ordem em que devem rodar: todas as
    repetições de uma célula antes de passar para a próxima — a
    aleatorização já está na ordem de `cell_ids`, repetições dentro da
    mesma célula não precisam de nova ordem embaralhada."""
    return [(cell_id, rep) for cell_id in cell_ids for rep in range(repetitions)]


def target_url_for(
    cell_id: str, target_url: str | None, targets: dict[str, str] | None
) -> str:
    if targets is not None:
        return targets[cell_id]
    if target_url is not None:
        return target_url
    raise ValueError("informe --target-url ou --targets")


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def run_k6(
    cell_id: str,
    repetition: int,
    target_url: str,
    phase: str,
    rate: int,
    k: int,
    selectivity_tier: str,
    smoke: bool,
    out_dir: Path,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_out = out_dir / "k6-raw.json"
    cmd = [
        "k6",
        "run",
        str(SCENARIOS_SCRIPT),
        "--out",
        f"json={json_out}",
        "-e",
        f"CELL={cell_id}",
        "-e",
        f"TARGET_URL={target_url}",
        "-e",
        f"RATE={rate}",
        "-e",
        f"K={k}",
        "-e",
        f"SELECTIVITY_TIER={selectivity_tier}",
    ]
    if smoke:
        # --vus/--duration na CLI do k6 são ignorados quando
        # options.scenarios já está definido no .js (é sempre o caso aqui) —
        # SMOKE_MODE=true troca o cenário inteiro dentro de scenarios.js
        # por um curto e sem threshold de SLO (ver load/scenarios.js).
        cmd += ["-e", "SMOKE_MODE=true"]

    manifest = {
        "cell_id": cell_id,
        "repetition": repetition,
        "phase": phase,
        "target_url": target_url,
        "rate": rate,
        "k": k,
        "selectivity_tier": selectivity_tier,
        "smoke": smoke,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    subprocess.run(cmd, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cells", nargs="*", default=None)
    parser.add_argument("--target-url")
    parser.add_argument("--targets", type=Path)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--phase", default="triagem", choices=["triagem", "confirmacao", "smoke"]
    )
    parser.add_argument("--rate", type=int, default=100)
    parser.add_argument("--k", type=int, default=20)
    parser.add_argument(
        "--selectivity-tier", default="medium", choices=["high", "medium", "low"]
    )
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args(argv)

    cell_ids = args.cells or list_viable_cell_ids()
    order = shuffled_cell_order(cell_ids, args.seed)
    print(f"ordem embaralhada (seed={args.seed}): {order}")

    targets = json.loads(args.targets.read_text()) if args.targets else None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for cell_id, repetition in build_run_plan(order, args.repetitions):
        url = target_url_for(cell_id, args.target_url, targets)
        out_dir = RESULTS_DIR / cell_id / args.phase / timestamp / f"rep{repetition}"
        run_k6(
            cell_id,
            repetition,
            url,
            args.phase,
            args.rate,
            args.k,
            args.selectivity_tier,
            args.smoke,
            out_dir,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
