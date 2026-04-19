import Database from "better-sqlite3";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";

const dbPath = process.env.DB_PATH ?? "/app/data/local-ai-stack.db";
mkdirSync(dirname(dbPath), { recursive: true });

export const db = new Database(dbPath);
db.pragma("journal_mode = WAL");
db.pragma("foreign_keys = ON");

export function initDb() {
  db.exec(`
    CREATE TABLE IF NOT EXISTS chats (
      id          TEXT PRIMARY KEY,
      user_email  TEXT NOT NULL,
      title       TEXT NOT NULL DEFAULT 'New chat',
      model_id    TEXT NOT NULL,
      thinking    INTEGER NOT NULL DEFAULT 0,
      created_at  INTEGER NOT NULL,
      updated_at  INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_chats_user ON chats(user_email, updated_at DESC);

    CREATE TABLE IF NOT EXISTS messages (
      id          TEXT PRIMARY KEY,
      chat_id     TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
      role        TEXT NOT NULL,
      content     TEXT NOT NULL,
      attachments TEXT,
      tokens      INTEGER,
      tokens_per_sec REAL,
      created_at  INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, created_at);

    CREATE TABLE IF NOT EXISTS uploads (
      id         TEXT PRIMARY KEY,
      user_email TEXT NOT NULL,
      filename   TEXT NOT NULL,
      mime_type  TEXT NOT NULL,
      size_bytes INTEGER NOT NULL,
      path       TEXT NOT NULL,
      created_at INTEGER NOT NULL
    );
  `);
}
