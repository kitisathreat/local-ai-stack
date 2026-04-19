import type { FastifyPluginAsync } from "fastify";
import { randomUUID } from "node:crypto";
import OpenAI from "openai";
import { db } from "../lib/db.js";
import { loadModels } from "../lib/models.js";

const lmStudio = new OpenAI({
  baseURL: process.env.LMSTUDIO_URL ?? "http://host.docker.internal:1234/v1",
  apiKey: "lmstudio",
});

export const messagesRoutes: FastifyPluginAsync = async (app) => {
  app.get<{ Params: { id: string } }>("/chats/:id/messages", async (req, reply) => {
    const chat = db.prepare(`SELECT user_email FROM chats WHERE id = ?`).get(req.params.id) as
      | { user_email: string } | undefined;
    if (!chat || chat.user_email !== req.userEmail) return reply.code(404).send({ error: "not_found" });

    const rows = db.prepare(`
      SELECT id, role, content, attachments, tokens, tokens_per_sec, created_at
      FROM messages WHERE chat_id = ? ORDER BY created_at ASC
    `).all(req.params.id);
    return { messages: rows };
  });

  app.post<{ Params: { id: string }; Body: { content: string; attachments?: string[] } }>(
    "/chats/:id/messages",
    async (req, reply) => {
      const chat = db.prepare(`SELECT user_email, model_id, thinking FROM chats WHERE id = ?`).get(req.params.id) as
        | { user_email: string; model_id: string; thinking: number } | undefined;
      if (!chat || chat.user_email !== req.userEmail) return reply.code(404).send({ error: "not_found" });

      const { profiles } = loadModels();
      const profile = profiles[chat.model_id];
      if (!profile) return reply.code(400).send({ error: "invalid_profile" });

      // Persist the user message
      const userMsgId = randomUUID();
      const now = Date.now();
      db.prepare(`
        INSERT INTO messages (id, chat_id, role, content, attachments, created_at)
        VALUES (?, ?, 'user', ?, ?, ?)
      `).run(userMsgId, req.params.id, req.body.content, JSON.stringify(req.body.attachments ?? []), now);

      // Load prior history
      const history = db.prepare(`
        SELECT role, content FROM messages WHERE chat_id = ? ORDER BY created_at ASC
      `).all(req.params.id) as { role: string; content: string }[];

      const systemPrompt = chat.thinking && profile.supports_thinking
        ? "You are a helpful assistant. Think step by step before responding."
        : "You are a helpful assistant.";

      // SSE setup
      reply.raw.writeHead(200, {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
      });
      const send = (event: string, data: unknown) => {
        reply.raw.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
      };

      const started = Date.now();
      let tokens = 0;
      let assistantContent = "";
      const assistantMsgId = randomUUID();

      try {
        const stream = await lmStudio.chat.completions.create({
          model: profile.id,
          temperature: profile.temperature,
          top_p: profile.top_p,
          max_tokens: profile.max_tokens,
          stream: true,
          messages: [
            { role: "system", content: systemPrompt },
            ...history.map((m) => ({ role: m.role as "user" | "assistant", content: m.content })),
          ],
        });

        for await (const chunk of stream) {
          const delta = chunk.choices[0]?.delta?.content ?? "";
          if (!delta) continue;
          assistantContent += delta;
          tokens += 1;
          send("token", { delta });
        }
      } catch (err) {
        send("error", { message: (err as Error).message });
        reply.raw.end();
        return reply;
      }

      const elapsed = (Date.now() - started) / 1000;
      const tps = elapsed > 0 ? tokens / elapsed : 0;

      db.prepare(`
        INSERT INTO messages (id, chat_id, role, content, tokens, tokens_per_sec, created_at)
        VALUES (?, ?, 'assistant', ?, ?, ?, ?)
      `).run(assistantMsgId, req.params.id, assistantContent, tokens, tps, Date.now());

      db.prepare(`UPDATE chats SET updated_at = ? WHERE id = ?`).run(Date.now(), req.params.id);

      send("done", { id: assistantMsgId, tokens, tokens_per_sec: tps });
      reply.raw.end();
      return reply;
    }
  );
};
