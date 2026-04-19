import type { FastifyPluginAsync, FastifyRequest } from "fastify";
import { createRemoteJWKSet, jwtVerify } from "jose";

declare module "fastify" {
  interface FastifyRequest {
    userEmail: string;
  }
}

const teamDomain = process.env.CF_ACCESS_TEAM_DOMAIN;
const audience   = process.env.CF_ACCESS_AUD;
const devEmail   = process.env.DEV_TRUSTED_EMAIL;

const jwks = teamDomain
  ? createRemoteJWKSet(new URL(`https://${teamDomain}/cdn-cgi/access/certs`))
  : null;

async function resolveEmail(req: FastifyRequest): Promise<string | null> {
  // Dev bypass — only when explicitly set AND no CF headers present
  if (devEmail && !req.headers["cf-access-jwt-assertion"]) {
    return devEmail;
  }

  if (!jwks || !audience) return null;

  const token = req.headers["cf-access-jwt-assertion"];
  if (typeof token !== "string") return null;

  try {
    const { payload } = await jwtVerify(token, jwks, { audience });
    const email = (payload.email ?? payload.identity_nonce) as string | undefined;
    return email ?? null;
  } catch {
    return null;
  }
}

export const authPlugin: FastifyPluginAsync = async (app) => {
  app.addHook("onRequest", async (req, reply) => {
    if (req.url === "/health") return;
    const email = await resolveEmail(req);
    if (!email) {
      reply.code(401).send({ error: "unauthorized" });
      return reply;
    }
    req.userEmail = email;
  });
};
