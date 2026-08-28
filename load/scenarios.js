// Gerador de carga (Etapa 7) contra service/http_app.py — ver CONTEXTO.md,
// "Protocolo de medição", e o plano em
// implementacao-md-com-base-nos-staged-snowflake.md, Etapa 7.
//
// Executor `constant-arrival-rate`: modelo aberto de verdade (taxa de
// chegada fixa, independente de quão rápido o serviço responde) — é o
// motivo de ter escolhido k6 sobre Locust (Locust é de malha fechada,
// produz omissão coordenada quando o serviço degrada; ver CONTEXTO.md,
// "Pilha").
//
// T-C (gRPC) fica de fora deste script por decisão já tomada na Etapa 6:
// só faz sentido comparar transporte (Fase 2) depois que a Fase 1 escolher
// a célula vencedora via medição real na nuvem — o que ainda não aconteceu.
// TRANSPORT aqui serve só para marcar T-A vs. T-B nas tags de resultado;
// T-B (HTTP/2 cleartext / h2c) exige nota à parte — ver abaixo.
//
// IMPORTANTE (limitação conhecida): o k6 negocia HTTP/2 automaticamente só
// sobre TLS (ALPN) — não tem suporte nativo a "prior knowledge" h2c em
// texto plano. service/http_app.py já expõe h2c real (verificado
// manualmente com a lib `h2`, ver README.md, Etapa 6), mas para o k6
// efetivamente falar H2 com ele, T-B vai precisar de TLS na frente do
// serviço (decisão adiada para quando a Fase 2 começar, já que ainda não
// escolhemos a célula vencedora da Fase 1). Por ora, TRANSPORT=http1 é o
// único caminho exercitado de ponta a ponta.

import http from 'k6/http';
import { check } from 'k6';
import { SharedArray } from 'k6/data';
import { Trend } from 'k6/metrics';
import exec from 'k6/execution';
import { sampleUserId } from './zipf.js';

// Métrica customizada: analysis/collect.py junta isto com http_req_duration
// pelo tag `request_id` para montar latencies.parquet (IMPLEMENTACAO.md,
// "Coleta de resultados": "uma linha por requisição: timestamp, latência,
// status, contagem de itens retornados") — o k6 não correlaciona métricas
// por si só, então o join precisa de um id explícito e único por requisição.
const returnedCount = new Trend('returned_count', false);

const TARGET_URL = __ENV.TARGET_URL || 'http://localhost:8000/v1/recommendations';
const CELL = __ENV.CELL || 'e1-postgres';
const K = parseInt(__ENV.K || '20', 10);
const RATE = parseInt(__ENV.RATE || '100', 10);
const SELECTIVITY_TIER = __ENV.SELECTIVITY_TIER || 'medium';
const TRANSPORT = __ENV.TRANSPORT || 'http1';
const EXCLUSION_SIZE = parseInt(__ENV.EXCLUSION_SIZE || '20', 10);
// I=87.585 é constante do catálogo (CONTEXTO.md, "Parâmetros fixos") — não
// escala com U, então não precisa de override entre local e nuvem.
const ITEM_COUNT = parseInt(__ENV.ITEM_COUNT || '87585', 10);
// Rampa até violar o SLO (CONTEXTO.md): cenário à parte, ativado por env,
// nunca simultâneo às taxas fixas.
const RAMP_MODE = (__ENV.RAMP_MODE || 'false') === 'true';
// Smoke test em nuvem (infra/scripts/cloud_smoke_test.py): só confirma que
// o serviço responde, não mede nada — poucas iterações, sem SLO. Cenário
// próprio (nome "smoke", fora de MEASUREMENT_SCENARIOS em
// analysis/collect.py) em vez de tentar encolher constantRateScenarios via
// --vus/--duration na CLI do k6, que o k6 ignora quando `options.scenarios`
// já está definido.
const SMOKE_MODE = (__ENV.SMOKE_MODE || 'false') === 'true';

const contextsByTier = new SharedArray('contexts_by_tier', function () {
  return [JSON.parse(open('./fixtures/contexts_by_tier.json'))];
})[0];

const CONTEXT_ID = contextsByTier[SELECTIVITY_TIER].context_id;

function randomExcludeIds() {
  const excluded = new Set();
  while (excluded.size < EXCLUSION_SIZE) {
    excluded.add(Math.floor(Math.random() * ITEM_COUNT));
  }
  return Array.from(excluded);
}

// VUs suficientes para sustentar RATE dado o orçamento de latência do SLO
// (p99 < 200 ms, CONTEXTO.md) com folga — ajustar durante as execuções
// reais na nuvem se o k6 acusar "dropped iterations".
const preAllocatedVUs = Math.max(50, Math.ceil(RATE * 0.5));
const maxVUs = Math.max(200, RATE * 2);

const constantRateScenarios = {
  warmup: {
    executor: 'constant-arrival-rate',
    rate: RATE,
    timeUnit: '1s',
    duration: '2m',
    preAllocatedVUs,
    maxVUs,
  },
  measurement: {
    executor: 'constant-arrival-rate',
    rate: RATE,
    timeUnit: '1s',
    duration: '5m',
    startTime: '2m',
    preAllocatedVUs,
    maxVUs,
  },
};

const rampScenarios = {
  ramp_to_slo: {
    executor: 'ramping-arrival-rate',
    startRate: 100,
    timeUnit: '1s',
    preAllocatedVUs: 1000,
    maxVUs: 8000,
    stages: [
      { target: 100, duration: '30s' },
      { target: 1000, duration: '1m' },
      { target: 5000, duration: '2m' },
      { target: 10000, duration: '2m' },
      { target: 20000, duration: '2m' },
    ],
  },
};

const smokeScenarios = {
  smoke: {
    executor: 'per-vu-iterations',
    vus: 1,
    iterations: 10,
    maxDuration: '30s',
  },
};

// `abortOnFail` só no modo rampa: é o mecanismo que implementa "rampa até
// violar o SLO" — para as taxas fixas queremos a janela de medição inteira
// mesmo que o SLO seja violado ocasionalmente, para a análise decidir. Smoke
// não tem threshold de SLO nenhum — só sanidade ("não deu erro"), não medição.
const thresholds = SMOKE_MODE
  ? {}
  : RAMP_MODE
    ? {
        'http_req_duration{scenario:ramp_to_slo}': [{ threshold: 'p(99)<200', abortOnFail: true }],
        'http_req_failed{scenario:ramp_to_slo}': [{ threshold: 'rate<0.01', abortOnFail: true }],
      }
    : {
        'http_req_duration{scenario:measurement}': ['p(99)<200'],
        'http_req_failed{scenario:measurement}': ['rate<0.01'],
      };

export const options = {
  scenarios: SMOKE_MODE ? smokeScenarios : RAMP_MODE ? rampScenarios : constantRateScenarios,
  thresholds,
};

export default function () {
  const requestId = `${exec.vu.idInTest}-${exec.vu.iterationInScenario}`;
  const payload = JSON.stringify({
    user_id: sampleUserId(),
    context: [CONTEXT_ID],
    exclude: randomExcludeIds(),
    k: K,
  });
  const params = {
    headers: { 'Content-Type': 'application/json' },
    tags: { cell: CELL, transport: TRANSPORT, tier: SELECTIVITY_TIER, request_id: requestId },
  };
  const res = http.post(TARGET_URL, payload, params);
  check(res, { 'status is 200': (r) => r.status === 200 });
  if (res.status === 200) {
    returnedCount.add(res.json('returned_count'), {
      request_id: requestId,
      scenario: exec.scenario.name,
    });
  }
}
