/**
 * parttime-monster-comments — an anonymous comment bridge for a static blog.
 *
 * Idea: the blog stays 100% static (GitHub Pages), comments live in GitHub
 * Discussions, and this worker is the only moving part. It is stateless:
 *
 *   GET  /api/comments?page=/posts/foo/   -> read the thread (edge-cached)
 *   POST /api/comments                    -> create a comment after checks
 *
 * Write path (POST):
 *   Turnstile CAPTCHA -> origin check -> honeypot -> rate limit (KV, 1/min/IP)
 *   -> trivial spam heuristics -> addDiscussionComment (or createDiscussion)
 *
 * Because discussions are titled with the page path, the very same threads
 * are served by giscus too — you can switch providers in site.toml without
 * losing a single comment.
 *
 * Secrets/vars (see README.md next to this file):
 *   GH_TOKEN            fine-grained PAT, Discussions: Read and write
 *   TURNSTILE_SECRET    Turnstile secret key (optional but recommended)
 *   GH_OWNER, GH_REPO   e.g. "asukhov", "test"
 *   GH_CATEGORY_ID      discussions category id (e.g. "Comments")
 *   ALLOWED_ORIGIN      comma separated origins, or "*"
 *   RATE_LIMIT          KV namespace binding (optional)
 */

const GH_API = "https://api.github.com/graphql";

const FIND_DISCUSSION = `
query ($q: String!) {
  search(query: $q, type: DISCUSSION, first: 5) {
    nodes {
      ... on Discussion {
        id
        title
        comments(first: 100) {
          nodes { id body createdAt }
        }
      }
    }
  }
}`;

const REPO_ID = `
query ($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) { id }
}`;

const CREATE_DISCUSSION = `
mutation ($repoId: ID!, $categoryId: ID!, $title: String!, $body: String!) {
  createDiscussion(input: { repositoryId: $repoId, categoryId: $categoryId, title: $title, body: $body }) {
    discussion { id }
  }
}`;

const ADD_COMMENT = `
mutation ($discussionId: ID!, $body: String!) {
  addDiscussionComment(input: { discussionId: $discussionId, body: $body }) {
    comment { id createdAt }
  }
}`;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    try {
      if (request.method === "OPTIONS") {
        return new Response(null, { status: 204, headers: cors(request, env) });
      }
      if (url.pathname === "/api/comments" && request.method === "GET") {
        return await getComments(request, env, ctx);
      }
      if (url.pathname === "/api/comments" && request.method === "POST") {
        return await postComment(request, env);
      }
      return json({ error: "not found" }, 404, cors(request, env));
    } catch (err) {
      return json({ error: "internal error" }, 500, cors(request, env));
    }
  }
};

/* ------------------------------------------------------------------ reads */

async function getComments(request, env, ctx) {
  const page = normalizePage(new URL(request.url).searchParams.get("page") || "");
  if (!page) return json({ error: "missing page" }, 400, cors(request, env));

  const cache = caches.default;
  const cached = await cache.match(request);
  if (cached) return cached;

  const discussion = await findDiscussion(page, env);
  const comments = discussion
    ? discussion.comments.nodes
        .map((c) => normalizeComment(c))
        .sort((a, b) => (a.created < b.created ? -1 : 1))
    : [];

  const response = json({ page, comments }, 200, {
    ...cors(request, env),
    "cache-control": "public, max-age=120, s-maxage=120"
  });
  if (ctx) ctx.waitUntil(cache.put(request, response.clone()));
  return response;
}

function normalizeComment(node) {
  const { author, body } = splitAuthor(node.body || "");
  return { id: node.id, created: node.createdAt, author, body };
}

/* the worker stores "**name**\n\ntext" — this splits it back apart */
function splitAuthor(body) {
  const m = body.match(/^\*\*(.+?)\*\*\s*\n+([\s\S]*)$/);
  if (m) return { author: m[1].trim(), body: m[2].trim() };
  return { author: "anonymous", body: body.trim() };
}

/* ----------------------------------------------------------------- writes */

async function postComment(request, env) {
  const origin = request.headers.get("origin") || "";
  if (!originAllowed(origin, env)) {
    return json({ error: "forbidden origin" }, 403, cors(request, env));
  }

  let payload;
  try {
    payload = await request.json();
  } catch {
    return json({ error: "bad json" }, 400, cors(request, env));
  }

  const page = normalizePage(String(payload.page || ""));
  const name = clean(String(payload.name || ""), 40);
  const text = clean(String(payload.body || ""), 4000);
  const trap = String(payload.website || "").trim();

  if (!page || !name || !text) {
    return json({ error: "page, name and body are required" }, 400, cors(request, env));
  }
  if (trap) return json({ ok: true }, 200, cors(request, env)); // honeypot: silent drop
  if ((text.match(/https?:\/\//g) || []).length > 3) {
    return json({ error: "too many links" }, 400, cors(request, env));
  }
  if (!(await verifyTurnstile(String(payload.token || ""), request, env))) {
    return json({ error: "captcha failed" }, 403, cors(request, env));
  }

  const ip = request.headers.get("cf-connecting-ip") || "";
  if (await rateLimited(ip, env)) {
    return json({ error: "slow down — one comment per minute" }, 429, cors(request, env));
  }

  const discussion = await findDiscussion(page, env);
  const discussionId = discussion
    ? discussion.id
    : await createDiscussion(page, env);
  if (!discussionId) return json({ error: "discussions unavailable" }, 500, cors(request, env));

  const body = `**${name}**\n\n${text}`;
  await gh(ADD_COMMENT, { discussionId, body }, env);
  return json({ ok: true }, 201, cors(request, env));
}

/* -------------------------------------------------------------- github api */

async function gh(query, variables, env) {
  const res = await fetch(GH_API, {
    method: "POST",
    headers: {
      authorization: `bearer ${env.GH_TOKEN}`,
      "content-type": "application/json",
      "user-agent": "parttime-monster-comments-worker"
    },
    body: JSON.stringify({ query, variables })
  });
  const data = await res.json();
  if (data.errors) throw new Error(JSON.stringify(data.errors));
  return data.data;
}

let repoIdCache = "";

async function repoId(env) {
  if (!repoIdCache) {
    const data = await gh(REPO_ID, { owner: env.GH_OWNER, repo: env.GH_REPO }, env);
    repoIdCache = data.repository.id;
  }
  return repoIdCache;
}

async function findDiscussion(page, env) {
  const q = `repo:${env.GH_OWNER}/${env.GH_REPO} in:title "${page}"`;
  const data = await gh(FIND_DISCUSSION, { q }, env);
  return data.search.nodes.find((n) => n && n.title === page) || null;
}

async function createDiscussion(page, env) {
  const data = await gh(CREATE_DISCUSSION, {
    repoId: await repoId(env),
    categoryId: env.GH_CATEGORY_ID,
    title: page,
    body: `Comments for <${page}>`
  }, env);
  return data.createDiscussion.discussion.id;
}

/* ------------------------------------------------------------------ guards */

async function verifyTurnstile(token, request, env) {
  if (!env.TURNSTILE_SECRET) return true; // unset: skip (use while testing)
  const form = new FormData();
  form.append("secret", env.TURNSTILE_SECRET);
  form.append("response", token);
  const ip = request.headers.get("cf-connecting-ip");
  if (ip) form.append("remoteip", ip);
  const res = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
    method: "POST",
    body: form
  });
  const data = await res.json();
  return !!data.success;
}

async function rateLimited(ip, env) {
  if (!env.RATE_LIMIT || !ip) return false;
  const key = `rl:${ip}`;
  if (await env.RATE_LIMIT.get(key)) return true;
  await env.RATE_LIMIT.put(key, "1", { expirationTtl: 60 });
  return false;
}

function originAllowed(origin, env) {
  const allowed = (env.ALLOWED_ORIGIN || "*").split(",").map((s) => s.trim());
  if (allowed.includes("*")) return true;
  return !!origin && allowed.includes(origin);
}

function normalizePage(page) {
  // keep it to a sane path, no queries or fragments
  const p = page.split(/[?#]/)[0];
  return /^\/[\w\-./]{1,200}$/.test(p) ? p : "";
}

function clean(text, max) {
  return text
    .replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, "")
    .replace(/\r\n/g, "\n")
    .trim()
    .slice(0, max);
}

/* ----------------------------------------------------------------- helpers */

function cors(request, env) {
  const origin = request.headers.get("origin") || "";
  return {
    "access-control-allow-origin": originAllowed(origin, env) ? origin || "*" : "null",
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-headers": "content-type",
    "vary": "origin"
  };
}

function json(data, status, headers) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...headers }
  });
}
