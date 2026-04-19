import type { FastifyPluginAsync } from "fastify";
import { randomUUID } from "node:crypto";
import { mkdirSync, createWriteStream } from "node:fs";
import { pipeline } from "node:stream/promises";
import { join } from "node:path";
import { db } from "../lib/db.js";

const uploadDir = process.env.UPLOAD_DIR ?? "/app/data/uploads";
mkdirSync(uploadDir, { recursive: true });

export const uploadsRoutes: FastifyPluginAsync = async (app) => {
  app.post("/uploads", async (req, reply) => {
    const file = await req.file();
    if (!file) return reply.code(400).send({ error: "no_file" });

    const id = randomUUID();
    const ext = file.filename.includes(".") ? file.filename.split(".").pop() : "bin";
    const path = join(uploadDir, `${id}.${ext}`);
    await pipeline(file.file, createWriteStream(path));

    const size = file.file.bytesRead;
    db.prepare(`
      INSERT INTO uploads (id, user_email, filename, mime_type, size_bytes, path, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?)
    `).run(id, req.userEmail, file.filename, file.mimetype, size, path, Date.now());

    return { id, filename: file.filename, mime_type: file.mimetype, size };
  });
};
