import type { FastifyPluginAsync } from "fastify";
import { loadModels } from "../lib/models.js";

export const modelsRoutes: FastifyPluginAsync = async (app) => {
  app.get("/models", async () => {
    const { default: defaultProfile, profiles } = loadModels();
    const list = Object.entries(profiles).map(([key, p]) => ({
      profile: key,
      name: p.name,
      description: p.description,
      context: p.context,
      supports_thinking: p.supports_thinking,
      supports_vision: p.supports_vision,
    }));
    return { default: defaultProfile, profiles: list };
  });
};
