// Amostragem de user_id por distribuição de Zipf (expoente 1,0 — fixo pelo
// desenho experimental, ver docs/DESIGN.md "Protocolo de medição": "Distribuição
// de acesso: Zipf com expoente 1,0 (não uniforme)"). Sem lib externa
// disponível no runtime JS do k6: a CDF é pré-computada uma única vez,
// normalizada, e amostrada por busca binária a cada requisição.
//
// USER_COUNT precisa ser passado por __ENV: localmente U=10.000, na nuvem
// U=1.000.000 (docs/DESIGN.md, "Parâmetros fixos") — nunca hardcoded aqui, já
// que os dois ambientes usam este mesmo script.
//
// `SharedArray` garante que a tabela seja computada uma vez por execução do
// k6, não uma vez por VU — para USER_COUNT=1.000.000 recomputar por VU
// desperdiçaria CPU e memória proporcionalmente ao número de VUs.

import { SharedArray } from 'k6/data';

const USER_COUNT = parseInt(__ENV.USER_COUNT || '10000', 10);
const ZIPF_EXPONENT = 1.0;

const zipfCdf = new SharedArray('zipf_cdf', function () {
  const weights = new Array(USER_COUNT);
  let sum = 0;
  for (let i = 0; i < USER_COUNT; i++) {
    weights[i] = 1 / Math.pow(i + 1, ZIPF_EXPONENT);
    sum += weights[i];
  }
  const cdf = new Array(USER_COUNT);
  let cumulative = 0;
  for (let i = 0; i < USER_COUNT; i++) {
    cumulative += weights[i] / sum;
    cdf[i] = cumulative;
  }
  return cdf;
});

// Menor índice i tal que cdf[i] >= target — inversão padrão de CDF discreta.
function bisect(cdf, target) {
  let lo = 0;
  let hi = cdf.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (cdf[mid] < target) {
      lo = mid + 1;
    } else {
      hi = mid;
    }
  }
  return lo;
}

export function sampleUserId() {
  return bisect(zipfCdf, Math.random());
}
