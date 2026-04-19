import Fastify from "fastify";
import cors from "@fastify/cors";
import multipart from "@fastify/multipart";
import { initDb } from "./lib/db.js";
import { authPlugin } from "./lib/auth.js";
import { healthRoutes } from "./routes/health.js";
import { modelsRoutes } from "./routes/models.js";
import { chatsRoutes } from "./routes/chats.js";
import { messagesRoutes } from "./routes/messages.js";
import { uploadsRoutes } from "./routes/uploads.js";
import { telemetryRoutes } from "./routes/telemetry.js";

const app = Fastify({ logger: { level: process.env.LOG_LEVEL ?? "info" } });

await app.register(cors, { origin: true, credentials: true });
await app.register(multipart, { limits: { fileSize: 50 * 1024 * 1024 } });

initDb();
await app.register(authPlugin);
await app.register(healthRoutes);
await app.register(modelsRoutes);
await app.register(chatsRoutes);
await app.register(messagesRoutes);
await app.register(uploadsRoutes);
await app.register(telemetryRoutes);

const port = Number(process.env.PORT ?? 8787);
await app.listen({ port, host: "0.0.0.0" });
app.log.info(`api listening on :${port}`);
