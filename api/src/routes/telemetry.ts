import type { FastifyPluginAsync } from "fastify";

const gpuAgentUrl = process.env.GPU_AGENT_URL ?? "http://host.docker.internal:8788/";
const lmStudioBase = (process.env.LMSTUDIO_URL ?? "http://host.docker.internal:1234/v1").replace(/\/v1\/?$/, "");

async function fetchGpu() {
  try {
    const r = await fetch(gpuAgentUrl, { signal: AbortSignal.timeout(1000) });
    if (r.ok) return await r.json();
  } catch {}
  return { gpu: "unavailable", overspill: "unknown" };
}

async function pingLmStudio() {
  const started = Date.now();
  try {
    await fetch(`${lmStudioBase}/v1/models`, { signal: AbortSignal.timeout(2000) });
    return Date.now() - started;
  } catch {
    return null;
  }
}

export const telemetryRoutes: FastifyPluginAsync = async (app) => {
  app.get("/telemetry", async (_req, reply) => {
    reply.raw.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    });

    let closed = false;
    reply.raw.on("close", () => { closed = true; });

    while (!closed) {
      const [gpu, ping] = await Promise.all([fetchGpu(), pingLmStudio()]);
      const payload = { gpu, ping_ms: ping, ts: Date.now() };
      reply.raw.write(`event: metrics\ndata: ${JSON.stringify(payload)}\n\n`);
      await new Promise((r) => setTimeout(r, 2000));
    }
    reply.raw.end();
    return reply;
  });
};
