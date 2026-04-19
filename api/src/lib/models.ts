import { readFileSync } from "node:fs";
import { parse } from "yaml";

export interface ModelProfile {
  id: string;
  name: string;
  description: string;
  context: number;
  temperature: number;
  top_p: number;
  top_k: number;
  repeat_penalty: number;
  max_tokens: number;
  supports_thinking: boolean;
  supports_vision: boolean;
}

const THINKING_HINTS = ["deepseek-r1", "qwen3-thinking", "reasoning"];
const VISION_HINTS   = ["llava", "qwen-vl", "llama-3.2-vision"];

export function loadModels(path = process.env.MODELS_YAML ?? "/app/config/models.yaml"): {
  default: string;
  profiles: Record<string, ModelProfile>;
} {
  const raw = parse(readFileSync(path, "utf8")) as {
    default: string;
    models: Record<string, Omit<ModelProfile, "id" | "supports_thinking" | "supports_vision"> & { id: string }>;
  };

  const profiles: Record<string, ModelProfile> = {};
  for (const [key, p] of Object.entries(raw.models)) {
    const idLower = p.id.toLowerCase();
    profiles[key] = {
      ...p,
      supports_thinking: THINKING_HINTS.some((h) => idLower.includes(h)),
      supports_vision:   VISION_HINTS.some((h) => idLower.includes(h)),
    };
  }
  return { default: raw.default, profiles };
}
