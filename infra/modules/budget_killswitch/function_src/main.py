# Cloud Function acionada pelo tópico Pub/Sub do orçamento
# (infra/envs/budget/main.tf: all_updates_rule) — trava de segurança, não
# só mais um alerta. Quando o gasto atinge KILL_THRESHOLD (hoje 1.2, em
# sincronia com o threshold_rules de 1.2 do orçamento), desliga o billing
# do projeto inteiro via projects.updateBillingInfo — cobre qualquer
# recurso que gere custo, não só as VMs que o Terraform conhece.
#
# DRY_RUN (env var, default "true" — ver infra/modules/budget_killswitch/
# main.tf: var.killswitch_dry_run) faz a function logar a decisão sem
# chamar a API de verdade. Nunca deveria valer "false" sem uma decisão
# explícita e testada (README.md, seção da trava de segurança).
#
# `decide()` é lógica pura, sem I/O nenhum — testável sem client GCP
# nenhum (ver test_main.py), assim como storage/tests separa lógica de
# adapters de fakes de infraestrutura.

import base64
import json
import logging
import os

import functions_framework
from google.cloud import billing_v1

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("budget_killswitch")

KILL_THRESHOLD = 1.2  # deve bater com threshold_rules { threshold_percent = 1.2 } em envs/budget/main.tf


def decide(cost_amount: float, budget_amount: float, billing_enabled: bool) -> str:
    """Decide a ação a partir dos números da notificação e do estado atual do billing.

    Retorna "ignore" (abaixo do limiar), "already_disabled" (limiar
    cruzado, mas billing já estava desligado — evita repetir a chamada
    enquanto o GCP reenvia a notificação periodicamente) ou "disable"
    (limiar cruzado, billing ligado, precisa agir agora).
    """
    if budget_amount <= 0:
        raise ValueError("budget_amount precisa ser positivo")
    if cost_amount / budget_amount < KILL_THRESHOLD:
        return "ignore"
    if not billing_enabled:
        return "already_disabled"
    return "disable"


def _parse_notification(cloud_event) -> dict:
    data = base64.b64decode(cloud_event.data["message"]["data"])
    return json.loads(data)


@functions_framework.cloud_event
def handle_budget_notification(cloud_event):
    notification = _parse_notification(cloud_event)
    project_id = os.environ["GOOGLE_CLOUD_PROJECT"]
    dry_run = os.environ.get("DRY_RUN", "true").lower() == "true"

    cost_amount = float(notification["costAmount"])
    budget_amount = float(notification["budgetAmount"])
    logger.info(
        "Notificação de orçamento: custo=%.2f orçamento=%.2f (%.1f%%)",
        cost_amount,
        budget_amount,
        100 * cost_amount / budget_amount,
    )

    billing_client = billing_v1.CloudBillingClient()
    project_name = f"projects/{project_id}"
    billing_info = billing_client.get_project_billing_info(name=project_name)

    action = decide(cost_amount, budget_amount, billing_info.billing_enabled)

    if action == "ignore":
        logger.info("Abaixo do limiar de %.0f%% — nenhuma ação.", KILL_THRESHOLD * 100)
        return
    if action == "already_disabled":
        logger.info(
            "Limiar de %.0f%% cruzado, mas o billing já está desabilitado — nenhuma ação.",
            KILL_THRESHOLD * 100,
        )
        return

    # action == "disable" — logar ANTES de desligar: depois da chamada,
    # o próprio Cloud Logging pode parar de receber (billing desligado).
    logger.critical(
        "LIMIAR DE %.0f%% CRUZADO — desligando o billing do projeto %s agora (dry_run=%s).",
        KILL_THRESHOLD * 100,
        project_id,
        dry_run,
    )
    if dry_run:
        logger.info("DRY_RUN=true — o billing NÃO foi desligado de verdade.")
        return

    billing_client.update_project_billing_info(
        name=project_name,
        project_billing_info=billing_v1.ProjectBillingInfo(billing_account_name=""),
    )
    logger.critical("Billing do projeto %s desabilitado.", project_id)
