import type { Metadata } from "next";
import { Suspense } from "react";

import { LoginForm } from "./login-form";

export const metadata: Metadata = { title: "Log in · Context Window" };

export default function LoginPage() {
  return (
    <main className="mx-auto w-full max-w-[420px] px-4 py-16 sm:px-6">
      <h1 className="font-heading text-3xl">Log in</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        For the editor: reviewing drafts, starting runs and voting on stories.
      </p>
      <Suspense>
        <LoginForm />
      </Suspense>
    </main>
  );
}
