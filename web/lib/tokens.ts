// Lightweight heuristic token estimator. A proper tiktoken WASM can be swapped
// in later; this is accurate enough for a context-window fill indicator.
// Empirically ~4 chars per token for English, less for code.
export function estimateTokens(text: string): number {
  if (!text) return 0;
  const chars = text.length;
  const words = text.trim().split(/\s+/).filter(Boolean).length;
  return Math.ceil(Math.max(chars / 4, words * 1.3));
}
