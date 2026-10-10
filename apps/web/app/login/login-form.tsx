"use client";

import { useSearchParams } from "next/navigation";

export function LoginForm() {
  const params = useSearchParams();
  const failed = params.get("error") === "1";
  return (
    <form action="/api/login" method="post" className="mt-8 space-y-4">
      <input
        type="hidden"
        name="next"
        value={params.get("next") ?? "/newsroom"}
      />
      <label className="block text-sm">
        <span className="text-muted-foreground">Password</span>
        <input
          name="password"
          type="password"
          required
          autoComplete="current-password"
          className="mt-1 block w-full rounded-md border border-input bg-background px-3 py-2 text-base outline-none focus:border-ring"
        />
      </label>
      {failed && (
        <p className="text-sm text-red-700 dark:text-red-400">
          That password is not right.
        </p>
      )}
      <button className="w-full rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground hover:opacity-90">
        Log in
      </button>
    </form>
  );
}
