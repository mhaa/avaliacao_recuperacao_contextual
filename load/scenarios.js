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
import exec from 'k6/execution';
import { sampleUserId } from './zipf.js';

// NÃO tagueie por requisição (nem aqui nem em params.tags abaixo). Cada
// valor único de tag vira uma série temporal nova no motor de métricas do
// k6 — confirmado ao vivo: com `request_id` único por requisição em
// params.tags, uma bateria real (1000 req/s por 7min contínuos) gerou
// centenas de milhares de séries ("could cause high memory usage", aviso do
// próprio k6) e o processo k6 afundou sob o peso das próprias métricas,
// produzindo p99 de 10-25s por requisição já ENFILEIRADA NO CLIENTE — não
// no serviço nem na rede (validado isolando cada camada: servidor e rede
// respondiam em ~1-2ms sob a mesma carga via requisições manuais). Endosso
// oficial do k6 para correlação por requisição é logging estruturado, não
// tags — https://github.com/grafana/k6/issues/2584 (suporte a tags de alta
// cardinalidade não-indexadas é só uma proposta em aberto, não existe na
// versão pinada aqui). Por isso returned_count e latência não são mais uma
// métrica k6 (Trend) — viram uma linha JSON por requisição em
// console.log(), capturada via `k6 run --console-output=<arquivo>` (nunca
// stdout puro: 1000 linhas/s por minutos poluiria e arriscaria interleaving
// no terminal). analysis/collect.py lê esse arquivo diretamente, sem
// precisar juntar duas métricas por tag.

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
// Sondagem de um único patamar (CONTEXTO.md, "Protocolo de medição" —
// vazão de saturação): usada tanto pela rampa curta exploratória da
// triagem quanto pela rampa fina de confirmação — a diferença entre elas
// (patamares, se tem aquecimento, duração) é decidida no orquestrador
// Python (load/saturation.py), nunca aqui. Substituiu o antigo RAMP_MODE/
// rampScenarios (um único ramp contínuo, sem busca binária nem checagem
// do gerador — não implementava o protocolo).
const PROBE_MODE = (__ENV.PROBE_MODE || 'false') === 'true';
const PROBE_RATE = parseInt(__ENV.PROBE_RATE || '100', 10);
const PROBE_WARMUP = __ENV.PROBE_WARMUP || '0s';
const PROBE_MEASURE = __ENV.PROBE_MEASURE || '1m';
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

const probeScenarios = {
  probe: {
    executor: 'constant-arrival-rate',
    rate: PROBE_RATE,
    timeUnit: '1s',
    duration: PROBE_MEASURE,
    startTime: PROBE_WARMUP,
    preAllocatedVUs: Math.max(50, Math.ceil(PROBE_RATE * 0.5)),
    maxVUs: Math.max(200, PROBE_RATE * 2),
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

// Sem threshold nenhum em SMOKE_MODE (só sanidade, "não deu erro", não
// medição) nem em PROBE_MODE (o Python decide violação de SLO olhando o
// summary calculado por analysis/collect.py depois — comparar contra o
// mesmo p99/taxa-de-erro que entra no manifest, em vez de duas
// implementações do mesmo julgamento, uma em JS outra em Python).
const thresholds =
  SMOKE_MODE || PROBE_MODE
    ? {}
    : {
        'http_req_duration{scenario:measurement}': ['p(99)<200'],
        'http_req_failed{scenario:measurement}': ['rate<0.01'],
      };

export const options = {
  scenarios: SMOKE_MODE ? smokeScenarios : PROBE_MODE ? probeScenarios : constantRateScenarios,
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
    tags: { cell: CELL, transport: TRANSPORT, tier: SELECTIVITY_TIER },
  };
  // Cronometrado manualmente (não http_req_duration do k6): http.post() é
  // síncrono dentro da iteração, então Date.now() antes/depois mede o
  // mesmo round-trip fim-a-fim que o k6 mediria — a única diferença é
  // incluir também http_req_blocked/connecting (setup de conexão), o que é
  // MAIS fiel à latência percebida pelo cliente, não menos.
  const t0 = Date.now();
  const res = http.post(TARGET_URL, payload, params);
  const latencyMs = Date.now() - t0;
  check(res, { 'status is 200': (r) => r.status === 200 });
  console.log(
    JSON.stringify({
      request_id: requestId,
      scenario: exec.scenario.name,
      timestamp: new Date().toISOString(),
      latency_ms: latencyMs,
      status: res.status,
      returned_count: res.status === 200 ? res.json('returned_count') : null,
    })
  );
}
