import { isAdmin } from "@/lib/session";

// Whether this browser has an admin session. Lets cached pages show admin controls
// without becoming per-user themselves.
export async function GET() {
  return Response.json(
    { admin: await isAdmin() },
    { headers: { "cache-control": "no-store" } },
  );
}
