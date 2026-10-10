import type { NextRequest } from "next/server";

import { API_URL } from "@/lib/api";
import { isAdmin } from "@/lib/session";

// The admin proxy: the browser calls /api/admin/<path> with its session cookie; this
// handler checks the session and forwards to the API with ADMIN_TOKEN, which therefore
// never leaves the server. Only these paths are forwarded.
const ALLOWED: { method: "GET" | "POST"; pattern: RegExp }[] = [
  { method: "GET", pattern: /^runs$/ },
  { method: "POST", pattern: /^runs$/ },
  { method: "GET", pattern: /^runs\/[0-9a-f-]{36}$/ },
  { method: "GET", pattern: /^runs\/[0-9a-f-]{36}\/events$/ },
  { method: "POST", pattern: /^runs\/[0-9a-f-]{36}\/review$/ },
  { method: "POST", pattern: /^stories\/[0-9a-f-]{36}\/feedback$/ },
];

async function forward(
  request: NextRequest,
  path: string[],
  method: "GET" | "POST",
) {
  if (!(await isAdmin())) {
    return Response.json({ detail: "log in first" }, { status: 401 });
  }
  const joined = path.join("/");
  if (
    !ALLOWED.some((rule) => rule.method === method && rule.pattern.test(joined))
  ) {
    return Response.json({ detail: "not allowed" }, { status: 404 });
  }
  const url = `${API_URL}/${joined}${request.nextUrl.search}`;
  const response = await fetch(url, {
    method,
    headers: {
      authorization: `Bearer ${process.env.ADMIN_TOKEN ?? ""}`,
      "content-type": "application/json",
    },
    body: method === "POST" ? await request.text() : undefined,
    cache: "no-store",
  });
  return new Response(await response.text(), {
    status: response.status,
    headers: {
      "content-type": "application/json",
      "cache-control": "no-store",
    },
  });
}

export async function GET(
  request: NextRequest,
  ctx: RouteContext<"/api/admin/[...path]">,
) {
  return forward(request, (await ctx.params).path, "GET");
}

export async function POST(
  request: NextRequest,
  ctx: RouteContext<"/api/admin/[...path]">,
) {
  return forward(request, (await ctx.params).path, "POST");
}
