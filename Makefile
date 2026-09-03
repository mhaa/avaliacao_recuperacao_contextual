# Fachada de comandos (docs/ARCHITECTURE.md, "Interface") — por baixo, sempre
# Docker/docker compose/python -m já documentados no README.md. Nenhuma
# lógica nova: cada alvo só encaminha, nunca reimplementa.
#
# Variáveis (defaults abaixo, sobrescreva na chamada — ex.: `make up
# DB=valkey`): SAMPLE, CELL, DB, PHASE, PROJECT_ID, REGION, ZONE,
# TF_STATE_BUCKET, RESULTS_BUCKET, DATASET_BUCKET, TOOLS_IMAGE, START.

SAMPLE ?= 10000
CELL ?= e1-postgres
DB ?= postgres
PHASE ?= triagem
REGION ?= us-central1
ZONE ?= us-central1-a

# cells/<id>.yaml segue sempre o padrão e<1-4>-<storage> — mesma
# convenção já usada em infra/scripts/cloud_smoke_test.py:storage_for_cell,
# não uma segunda fonte de verdade.
STORAGE := $(word 2,$(subst -, ,$(CELL)))

SCHEMA_SCRIPT_postgres = schemas/postgres/apply_schema.py
SCHEMA_SCRIPT_scylla = schemas/scylla/apply_schema.py
SCHEMA_SCRIPT_opensearch = schemas/opensearch/create_index.py
SCHEMA_SCRIPT := $(SCHEMA_SCRIPT_$(STORAGE))

.PHONY: help gen-data up load-schema verify verify-all smoke down analyze \
        tf-plan tf-apply tf-destroy measure saturation-triagem saturation-confirmacao

help:
	@echo "Ambiente local:  gen-data up load-schema verify verify-all smoke down analyze"
	@echo "Nuvem:           tf-plan tf-apply tf-destroy measure"
	@echo "Vazao saturacao: saturation-triagem saturation-confirmacao"
	@echo "Variaveis (ex.: make up DB=valkey): SAMPLE CELL DB PHASE PROJECT_ID REGION ZONE"
	@echo "  TF_STATE_BUCKET RESULTS_BUCKET DATASET_BUCKET TOOLS_IMAGE START"

## Ambiente local (Docker Compose) — README.md, Fase 1-3. Nunca medir aqui.

gen-data:
	docker compose build generator
	docker compose run --rm generator all --sample-users $(SAMPLE) --seed 42

up:
	docker compose up -d $(DB)

load-schema:
	docker compose build tools
ifneq ($(SCHEMA_SCRIPT),)
	docker compose run --rm --entrypoint python tools $(SCHEMA_SCRIPT)
endif
	docker compose run --rm --entrypoint python tools schemas/$(STORAGE)/load_oracle_fixture.py

verify:
	docker compose run --rm tools -m integration tests/acceptance/test_harness_all_cells.py -v -k $(CELL)

verify-all:
	docker compose run --rm tools -m integration tests/acceptance/test_harness_all_cells.py -v

smoke:
	docker compose run --rm --entrypoint python tools load/export_contexts_by_tier.py
	docker compose run --rm tools -m integration -v

down:
	docker compose down

analyze:
	docker compose run --rm tools -m "not integration" analysis/tests -v

## Nuvem — Terraform + medição real (README.md, Fase 4-5). Tudo aqui é
## faturável; cada script pede confirmação antes de apply/destroy.

tf-plan:
	infra/scripts/with_terraform_credentials.sh $(PROJECT_ID) -- \
	  docker compose -f docker-compose.yml -f docker-compose.gcp.yml \
	  run --rm --entrypoint terraform tools -chdir=infra/envs/experiment plan \
	  -var=project_id=$(PROJECT_ID) -var=region=$(REGION) -var=zone=$(ZONE) \
	  -var=cell=$(CELL) -var=storage=$(STORAGE) -var=dataset_bucket=$(DATASET_BUCKET)

tf-apply:
	infra/scripts/with_terraform_credentials.sh $(PROJECT_ID) -- \
	  docker compose -f docker-compose.yml -f docker-compose.gcp.yml \
	  run --rm --entrypoint terraform tools -chdir=infra/envs/experiment apply \
	  -var=project_id=$(PROJECT_ID) -var=region=$(REGION) -var=zone=$(ZONE) \
	  -var=cell=$(CELL) -var=storage=$(STORAGE) -var=dataset_bucket=$(DATASET_BUCKET)

tf-destroy:
	infra/scripts/with_terraform_credentials.sh $(PROJECT_ID) -- \
	  docker compose -f docker-compose.yml -f docker-compose.gcp.yml \
	  run --rm --entrypoint terraform tools -chdir=infra/envs/experiment destroy \
	  -var=project_id=$(PROJECT_ID) -var=region=$(REGION) -var=zone=$(ZONE) \
	  -var=cell=$(CELL) -var=storage=$(STORAGE) -var=dataset_bucket=$(DATASET_BUCKET)

# Bateria completa de uma fase (carga fixa + a rampa de saturação
# correspondente, ver infra/scripts/run_measurement_battery.py) — PHASE=
# triagem não precisa de START; PHASE=confirmacao exige START (leia do
# report.json da triagem).
measure:
	TOOLS_IMAGE=$(TOOLS_IMAGE) python -m infra.scripts.run_measurement_battery \
	    $(CELL) $(PROJECT_ID) $(REGION) $(ZONE) $(TF_STATE_BUCKET) $(RESULTS_BUCKET) $(DATASET_BUCKET) \
	    --phase $(PHASE) $(if $(START),--saturation-start $(START),)

## Vazão de saturação (docs/DESIGN.md, "Delineamento em duas etapas" — 3ª
## dimensão da fronteira de Pareto) — alvos nomeados pedidos explicitamente,
## equivalentes a `measure PHASE=triagem`/`measure PHASE=confirmacao`.

saturation-triagem:
	$(MAKE) measure PHASE=triagem

saturation-confirmacao:
	$(MAKE) measure PHASE=confirmacao START=$(START)
