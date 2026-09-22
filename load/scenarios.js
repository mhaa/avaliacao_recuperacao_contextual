// Gerador de carga (Etapa 7) contra service/http_app.py — ver docs/DESIGN.md,
// "Protocolo de medição", e o plano em
// implementacao-md-com-base-nos-staged-snowflake.md, Etapa 7.
//
// Executor `constant-arrival-rate`: modelo aberto de verdade (taxa de
// chegada fixa, independente de quão rápido o serviço responde) — é o
// motivo de ter escolhido k6 sobre Locust (Locust é de malha fechada,
// produz omissão coordenada quando o serviço degrada; ver docs/DESIGN.md,
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
// I=87.585 é constante do catálogo (docs/DESIGN.md, "Parâmetros fixos") — não
// escala com U, então não precisa de override entre local e nuvem.
const ITEM_COUNT = parseInt(__ENV.ITEM_COUNT || '87585', 10);
// Sondagem de um único patamar (docs/DESIGN.md, "Protocolo de medição" —
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
// (p99 < 200 ms, docs/DESIGN.md) com folga — ajustar durante as execuções
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

// PROBE_WARMUP é um cenário de TRÁFEGO REAL na mesma taxa, descartado pela
// coleta (analysis/collect.py e analysis/probe_report.py filtram por
// scenario == 'probe'; 'probe_warmup' fica de fora, como 'warmup' na carga
// fixa). A versão anterior usava PROBE_WARMUP só como startTime do cenário
// único — 2 min de OCIOSIDADE, não de aquecimento: a medição da rampa de
// confirmação começava a frio (conexões novas, pools vazios) e as leituras
// de /proc/stat que cercam a sondagem diluíam a CPU do gerador com o tempo
// parado, afrouxando o portão dos 60% do docs/DESIGN.md.
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
if (PROBE_MODE && PROBE_WARMUP !== '0s') {
  probeScenarios.probe_warmup = {
    executor: 'constant-arrival-rate',
    rate: PROBE_RATE,
    timeUnit: '1s',
    duration: PROBE_WARMUP,
    preAllocatedVUs: Math.max(50, Math.ceil(PROBE_RATE * 0.5)),
    maxVUs: Math.max(200, PROBE_RATE * 2),
  };
}

const smokeScenarios = {
  smoke: {
    executor: 'per-vu-iterations',
    vus: 1,
    iterations: 10,
    maxDuration: '30s',
  },
};

// Rampa de estresse com foco no banco (docs/DESIGN.md, "Experimento
// complementar"). UMA execução contínua do k6, não um degrau por processo:
// reiniciar o k6 entre degraus daria ao banco janelas de ociosidade para
// drenar fila, destruindo justamente o fenômeno sob observação (se ele se
// recupera ou se perde).
//
// O cronograma inteiro vem pronto do Python (load/ramp.py:build_step_schedule)
// em RAMP_STAGES, em vez de ser recalculado aqui: manter a geometria da rampa
// em dois lugares garantiria divergência, e é o lado Python que já é testado
// e que faz o pré-voo de disco sobre esse mesmo cronograma.
const STRESS_RAMP_MODE = (__ENV.STRESS_RAMP_MODE || 'false') === 'true';
const RAMP_STAGES = JSON.parse(__ENV.RAMP_STAGES || '[]');
// Dimensionado explicitamente, NÃO pela heurística `RATE * 2` acima: a 50k
// req/s ela pediria 100.000 VUs, e o custo de memória por VU faria o próprio
// gerador derrubar o offered_ratio — envenenando em silêncio exatamente a
// medição em questão (o mesmo modo de falha do excesso de séries de métricas
// documentado no topo deste arquivo).
const RAMP_MAX_VUS = parseInt(__ENV.RAMP_MAX_VUS || '2000', 10);

// Fronteiras cumulativas dos degraus, em ms desde o início do teste.
// Calculadas uma vez no carregamento do módulo (não por iteração).
const rampBoundaries = [];
{
  let elapsed = 0;
  for (const stage of RAMP_STAGES) {
    elapsed += stage.duration_s * 1000;
    rampBoundaries.push(elapsed);
  }
}

// Atribuição de degrau pelo VALOR-VERDADE do cronograma, não por bucketing de
// timestamp na análise (docs/DECISIONS.md: atribuição sempre por dado
// estruturado). Um só cenário k6 cobre a rampa inteira, então a tag `scenario`
// não distingue degraus — este campo é que distingue.
function currentStep() {
  const elapsedMs = exec.instance.currentTestRunDuration;
  for (let i = 0; i < rampBoundaries.length; i++) {
    if (elapsedMs < rampBoundaries[i]) {
      return RAMP_STAGES[i];
    }
  }
  return RAMP_STAGES[RAMP_STAGES.length - 1];
}

// Estágios PAREADOS: `{target, duration:'0s'}` seguido de `{target, duration}`
// produz platôs quadrados. Sem o par de 0s, `ramping-arrival-rate` interpola
// linearmente entre alvos e não haveria degrau nenhum — só uma diagonal, na
// qual "a vazão sustentada no patamar X" não existiria como grandeza.
const rampStages = [];
for (const stage of RAMP_STAGES) {
  rampStages.push({ target: stage.rate, duration: '0s' });
  rampStages.push({ target: stage.rate, duration: `${stage.duration_s}s` });
}

const stressRampScenarios = {
  stress_ramp: {
    executor: 'ramping-arrival-rate',
    startRate: RAMP_STAGES.length > 0 ? RAMP_STAGES[0].rate : 1,
    timeUnit: '1s',
    stages: rampStages,
    preAllocatedVUs: Math.min(Math.max(50, Math.ceil(RAMP_MAX_VUS * 0.25)), RAMP_MAX_VUS),
    maxVUs: RAMP_MAX_VUS,
  },
};

// Sem threshold nenhum em SMOKE_MODE (só sanidade, "não deu erro", não
// medição) nem em PROBE_MODE (o Python decide violação de SLO olhando o
// summary calculado por analysis/collect.py depois — comparar contra o
// mesmo p99/taxa-de-erro que entra no manifest, em vez de duas
// implementações do mesmo julgamento, uma em JS outra em Python).
// STRESS_RAMP_MODE também roda sem threshold, e por um motivo mais forte que
// os outros dois: a rampa PRECISA continuar depois da violação do SLO — é o
// que se quer observar. Um threshold com abortOnFail encerraria a execução
// exatamente no ponto de interesse, e sem abortOnFail só sujaria o código de
// saída de uma execução bem-sucedida.
const thresholds =
  SMOKE_MODE || PROBE_MODE || STRESS_RAMP_MODE
    ? {}
    : {
        'http_req_duration{scenario:measurement}': ['p(99)<200'],
        'http_req_failed{scenario:measurement}': ['rate<0.01'],
      };

export const options = {
  scenarios: SMOKE_MODE
    ? smokeScenarios
    : STRESS_RAMP_MODE
      ? stressRampScenarios
      : PROBE_MODE
        ? probeScenarios
        : constantRateScenarios,
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
  const line = {
    scenario: exec.scenario.name,
    timestamp: new Date().toISOString(),
    latency_ms: latencyMs,
    status: res.status,
    returned_count: res.status === 200 ? res.json('returned_count') : null,
  };
  if (STRESS_RAMP_MODE) {
    // `step_rate` é a carga OFERTADA do degrau — o denominador de
    // offered_ratio em analysis/ramp_report.py. `request_id` fica de fora:
    // nenhum código de produção o lê (analysis/collect.py só o menciona num
    // comentário sobre uma abordagem abandonada), e numa rampa que emite
    // centenas de milhões de linhas cada byte por linha vira GB de disco.
    const step = currentStep();
    line.step_rate = step.rate;
    line.step_phase = step.phase;
  } else {
    line.request_id = requestId;
  }
  console.log(JSON.stringify(line));
}
