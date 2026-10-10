import { revalidateTag } from "next/cache";
import { timingSafeEqual } from "node:crypto";

// POST /api/revalidate: called by the newsroom's publish step (REVALIDATE_SECRET), so a new
// briefing shows at once instead of when the cache would expire.
export async function POST(request: Request) {
  const expected = process.env.REVALIDATE_SECRET ?? "";
  const given = (request.headers.get("authorization") ?? "").replace(
    /^Bearer\s+/i,
    "",
  );
  const ok =
    expected.length > 0 &&
    given.length === expected.length &&
    timingSafeEqual(Buffer.from(given), Buffer.from(expected));
  if (!ok) return Response.json({ error: "unauthorized" }, { status: 401 });
  revalidateTag("briefings", { expire: 0 }); // never serve the old briefing again
  return Response.json({ revalidated: true });
}
