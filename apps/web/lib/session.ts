// The admin session: an httpOnly, Secure, SameSite=Lax cookie holding an expiry time
// signed with HMAC-SHA256 (ADMIN_SESSION_SECRET). Server only: never import this file
// from a client component.

import { createHmac, timingSafeEqual } from "node:crypto";

import { cookies } from "next/headers";

export const SESSION_COOKIE = "cw_admin";
const MAX_AGE_SECONDS = 60 * 60 * 24 * 14; // two weeks

function secret(): string {
  const value = process.env.ADMIN_SESSION_SECRET;
  if (!value || value.length < 32) {
    throw new Error(
      "ADMIN_SESSION_SECRET must be set (at least 32 characters)",
    );
  }
  return value;
}

function sign(payload: string): string {
  return createHmac("sha256", secret()).update(payload).digest("base64url");
}

export function newSession(now = Date.now()): {
  value: string;
  maxAge: number;
} {
  const expires = String(Math.floor(now / 1000) + MAX_AGE_SECONDS);
  return { value: `${expires}.${sign(expires)}`, maxAge: MAX_AGE_SECONDS };
}

export function validSession(
  value: string | undefined,
  now = Date.now(),
): boolean {
  if (!value) return false;
  const [expires, signature] = value.split(".");
  if (!expires || !signature) return false;
  const expected = Buffer.from(sign(expires));
  const given = Buffer.from(signature);
  if (expected.length !== given.length || !timingSafeEqual(expected, given))
    return false;
  return Number(expires) * 1000 > now;
}

export async function isAdmin(): Promise<boolean> {
  return validSession((await cookies()).get(SESSION_COOKIE)?.value);
}

export function passwordMatches(given: string): boolean {
  const expected = process.env.ADMIN_PASSWORD;
  if (!expected) return false;
  const a = Buffer.from(given);
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

export const cookieOptions = {
  httpOnly: true,
  secure: process.env.NODE_ENV === "production",
  sameSite: "lax" as const,
  path: "/",
};
