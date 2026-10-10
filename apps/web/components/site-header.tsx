"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Suspense } from "react";
import { useAdmin } from "@/hooks/use-admin";
import { chooseTheme, type Theme, useThemeChoice } from "@/hooks/use-theme";

const LINKS = [
  { href: "/", label: "Briefing" },
  { href: "/briefings", label: "Archive" },
  { href: "/newsroom", label: "Newsroom" },
];

function ThemeToggle() {
  const theme = useThemeChoice();
  const next: Theme =
    theme === "system" ? "dark" : theme === "dark" ? "light" : "system";
  return (
    <button
      type="button"
      onClick={() => chooseTheme(next)}
      className="rounded-md px-2 py-1 text-muted-foreground hover:text-foreground"
      aria-label={`Theme: ${theme}. Switch to ${next}.`}
      title={`Theme: ${theme}`}
    >
      {theme === "dark" ? "Dark" : theme === "light" ? "Light" : "Auto"}
    </button>
  );
}

function NavLinks({ pathname }: { pathname: string | null }) {
  return (
    <nav className="flex items-center gap-4 text-sm">
      {LINKS.map((link) => {
        const active =
          pathname !== null &&
          (link.href === "/"
            ? pathname === "/"
            : pathname.startsWith(link.href));
        return (
          <Link
            key={link.href}
            href={link.href}
            className={
              active
                ? "text-foreground"
                : "text-muted-foreground hover:text-foreground"
            }
          >
            {link.label}
          </Link>
        );
      })}
    </nav>
  );
}

// The current path is runtime data on dynamic routes, so it streams in (Cache Components).
function ActiveNavLinks() {
  return <NavLinks pathname={usePathname()} />;
}

export function SiteHeader() {
  const admin = useAdmin();
  return (
    <header className="border-b border-border">
      <div className="mx-auto flex w-full max-w-[1100px] flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3 sm:px-6">
        <Link href="/" className="font-heading text-lg">
          Context Window
        </Link>
        <Suspense fallback={<NavLinks pathname={null} />}>
          <ActiveNavLinks />
        </Suspense>
        <div className="ml-auto flex items-center gap-2 text-sm">
          <ThemeToggle />
          {admin ? (
            <form action="/api/logout" method="post">
              <button className="rounded-md px-2 py-1 text-muted-foreground hover:text-foreground">
                Log out
              </button>
            </form>
          ) : (
            <Link
              href="/login"
              className="rounded-md px-2 py-1 text-muted-foreground hover:text-foreground"
            >
              Log in
            </Link>
          )}
        </div>
      </div>
    </header>
  );
}
