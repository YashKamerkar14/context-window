"use client";

import { useEffect, useState } from "react";

// Whether this browser has an admin session (asked of /api/session, which reads the
// httpOnly cookie on the server). False until known.
export function useAdmin(): boolean {
  const [admin, setAdmin] = useState(false);
  useEffect(() => {
    let live = true;
    fetch("/api/session", { cache: "no-store" })
      .then((r) => r.json())
      .then(
        (body: { admin?: boolean }) => live && setAdmin(Boolean(body.admin)),
      )
      .catch(() => live && setAdmin(false));
    return () => {
      live = false;
    };
  }, []);
  return admin;
}
