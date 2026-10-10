import { NextResponse, type NextRequest } from "next/server";

import {
  SESSION_COOKIE,
  cookieOptions,
  newSession,
  passwordMatches,
} from "@/lib/session";

// POST /api/login (a form): the right password sets the admin session cookie.
export async function POST(request: NextRequest) {
  const form = await request.formData();
  const password = String(form.get("password") ?? "");
  const next = String(form.get("next") ?? "/newsroom");
  const target =
    next.startsWith("/") && !next.startsWith("//") ? next : "/newsroom";
  if (!passwordMatches(password)) {
    return NextResponse.redirect(new URL("/login?error=1", request.url), 303);
  }
  const session = newSession();
  const response = NextResponse.redirect(new URL(target, request.url), 303);
  response.cookies.set(SESSION_COOKIE, session.value, {
    ...cookieOptions,
    maxAge: session.maxAge,
  });
  return response;
}
