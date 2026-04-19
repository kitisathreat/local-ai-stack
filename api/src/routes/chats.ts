import type { FastifyPluginAsync } from "fastify";
import { randomUUID } from "node:crypto";
import { db } from "../lib/db.js";
import { loadModels } from "../lib/models.js";

export const chatsRoutes: FastifyPluginAsync = async (app) => {
  app.get("/chats", async (req) => {
    const rows = db.prepare(`
      SELECT id, title, model_id, thinking, created_at, updated_at
      FROM chats WHERE user_email = ? ORDER BY updated_at DESC
    `).all(req.userEmail);
    return { chats: rows };
  });

  app.post<{ Body: { profile?: string; thinking?: boolean } }>("/chats", async (req) => {
    const { default: defaultProfile } = loadModels();
    const profile = req.body?.profile ?? defaultProfile;
    const id = randomUUID();
    const now = Date.now();
    db.prepare(`
      INSERT INTO chats (id, user_email, title, model_id, thinking, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?)
    `).run(id, req.userEmail, "New chat", profile, req.body?.thinking ? 1 : 0, now, now);
    return { id, profile };
  });

  app.patch<{ Params: { id: string }; Body: { title?: string; profile?: string; thinking?: boolean } }>(
    "/chats/:id",
    async (req, reply) => {
      const row = db.prepare(`SELECT user_email FROM chats WHERE id = ?`).get(req.params.id) as
        | { user_email: string } | undefined;
      if (!row || row.user_email !== req.userEmail) return reply.code(404).send({ error: "not_found" });

      const fields: string[] = [];
      const values: unknown[] = [];
      if (req.body.title !== undefined)    { fields.push("title = ?");    values.push(req.body.title); }
      if (req.body.profile !== undefined)  { fields.push("model_id = ?"); values.push(req.body.profile); }
      if (req.body.thinking !== undefined) { fields.push("thinking = ?"); values.push(req.body.thinking ? 1 : 0); }
      if (!fields.length) return { ok: true };
      fields.push("updated_at = ?"); values.push(Date.now());
      values.push(req.params.id);
      db.prepare(`UPDATE chats SET ${fields.join(", ")} WHERE id = ?`).run(...values);
      return { ok: true };
    }
  );

  app.delete<{ Params: { id: string } }>("/chats/:id", async (req, reply) => {
    const info = db.prepare(`DELETE FROM chats WHERE id = ? AND user_email = ?`).run(req.params.id, req.userEmail);
    if (info.changes === 0) return reply.code(404).send({ error: "not_found" });
    return { ok: true };
  });
};
