// Distributions by inverse CDF (the same uniform gives common random numbers) and the mulberry32 generator.
// A port of model-simulator.html ("distributions").
import type { Dist } from './types.js';

const QC = new Map<string, { cdf: Float64Array; G: number }>();

/** Clear the PERT table cache (the page clears it at the start of each simulation). */
export function clearQuantileCache(): void {
  QC.clear();
}

export function pertQ(a: number, m: number, c: number, u: number): number {
  if (!(c > a)) return a;
  m = Math.min(Math.max(m, a), c);
  return pertLookup(a, c, pertTable(a, m, c), u);
}

/** The CDF table of PERT(a, m, c) on a grid of 2048 cells (cached), m already inside [a, c]. */
function pertTable(a: number, m: number, c: number): { cdf: Float64Array; G: number } {
  const key = a + '|' + m + '|' + c;
  let T = QC.get(key);
  if (!T) {
    const al = 1 + 4 * (m - a) / (c - a), be = 1 + 4 * (c - m) / (c - a), G = 2048;
    const cdf = new Float64Array(G + 1);
    let acc = 0;
    for (let k = 1; k <= G; k++) {
      const x = (k - 0.5) / G;
      acc += Math.pow(x, al - 1) * Math.pow(1 - x, be - 1);
      cdf[k] = acc;
    }
    for (let k = 1; k <= G; k++) cdf[k] /= acc;
    T = { cdf, G };
    QC.set(key, T);
  }
  return T;
}

function pertLookup(a: number, c: number, T: { cdf: Float64Array; G: number }, u: number): number {
  let lo = 0, hi = T.G;
  while (hi - lo > 1) {
    const md = (lo + hi) >> 1;
    if (T.cdf[md] < u) lo = md;
    else hi = md;
  }
  return a + (c - a) * (lo + (u - T.cdf[lo]) / Math.max(T.cdf[hi] - T.cdf[lo], 1e-12)) / T.G;
}

/** quant(d, ·) with the PERT table looked up once: the same values, for the inner loop of the simulation. */
export function sampler(d: Dist): (u: number) => number {
  if (d.kind === 'fixed') {
    const v = d.value as number;
    return () => v;
  }
  if (d.kind === 'pert') {
    const a = d.low as number, c = d.high as number;
    if (!(c > a)) return () => a;
    const T = pertTable(a, Math.min(Math.max(d.mode as number, a), c), c);
    return u => pertLookup(a, c, T, u);
  }
  return u => quant(d, u);
}

/** Acklam's rational approximation of the normal inverse CDF. */
export function ndtri(p: number): number {
  const a = [-39.69683028665376, 220.9460984245205, -275.9285104469687, 138.357751867269, -30.66479806614716, 2.506628277459239];
  const b = [-54.47609879822406, 161.5858368580409, -155.6989798598866, 66.80131188771972, -13.28068155288572];
  const c = [-0.007784894002430293, -0.3223964580411365, -2.400758277161838, -2.549732539343734, 4.374664141464968, 2.938163982698783];
  const d = [0.007784695709041462, 0.3224671290700398, 2.445134137142996, 3.754408661907416];
  const pl = 0.02425;
  let q: number, r: number;
  if (p < pl) {
    q = Math.sqrt(-2 * Math.log(p));
    return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1);
  }
  if (p > 1 - pl) {
    q = Math.sqrt(-2 * Math.log(1 - p));
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1);
  }
  q = p - 0.5;
  r = q * q;
  return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1);
}

const clampU = (u: number) => Math.min(Math.max(u, 1e-12), 1 - 1e-12);

/** The value of distribution d at the uniform u. */
export function quant(d: Dist, u: number): number {
  switch (d.kind) {
    case 'fixed':
      return d.value as number;
    case 'pert':
      return pertQ(d.low as number, d.mode as number, d.high as number, u);
    case 'triangular': {
      const a = d.low as number, m = d.mode as number, c = d.high as number;
      if (!(c > a)) return a;
      const f = (m - a) / (c - a);
      return u < f ? a + Math.sqrt(u * (c - a) * (m - a)) : c - Math.sqrt((1 - u) * (c - a) * (c - m));
    }
    case 'uniform':
      return (d.low as number) + ((d.high as number) - (d.low as number)) * u;
    case 'normal':
      return (d.mean as number) + (d.std as number) * ndtri(clampU(u));
    case 'lognormal': {
      const mean = d.mean as number, std = d.std as number;
      const s2 = Math.log(1 + (std / mean) ** 2);
      return Math.exp(Math.log(mean) - s2 / 2 + Math.sqrt(s2) * ndtri(clampU(u)));
    }
    case 'bernoulli':
      return u > 1 - (d.p as number) ? 1 : 0;
  }
  throw new Error('unknown distribution ' + (d as Dist).kind);
}

/** mulberry32: a 32-bit seeded generator of uniforms in [0, 1). */
export function mulberry32(a: number): () => number {
  return () => {
    a |= 0;
    a = a + 0x6D2B79F5 | 0;
    let t = Math.imul(a ^ a >>> 15, 1 | a);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}

/** A representative value of a distribution: value, else mode, else mean, else 0 (the page's varVal). */
export function centre(d: Dist): number {
  return d.value ?? d.mode ?? d.mean ?? 0;
}
