import type { Metadata } from "next";
import { Geist, Geist_Mono, Source_Serif_4 } from "next/font/google";
import { SiteHeader } from "@/components/site-header";

import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

const sourceSerif = Source_Serif_4({
  variable: "--font-source-serif",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Context Window",
  description: "A verified daily briefing on AI for engineers.",
};

// Runs before paint: the saved theme ("light", "dark" or "system"), else the system's.
const themeScript = `(() => {
  let saved = "system";
  try { saved = localStorage.getItem("theme") || "system"; } catch {}
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const apply = () => {
    const dark = saved === "dark" || (saved === "system" && media.matches);
    document.documentElement.classList.toggle("dark", dark);
  };
  apply();
  media.addEventListener("change", () => {
    try { saved = localStorage.getItem("theme") || "system"; } catch {}
    apply();
  });
})();`;

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} ${sourceSerif.variable} h-full antialiased`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="flex min-h-full flex-col">
        <SiteHeader />
        <div className="flex-1">{children}</div>
        <footer className="mx-auto w-full max-w-[720px] px-4 py-10 text-xs text-muted-foreground sm:px-6">
          Every claim in a published story was checked against its source before
          publication.
        </footer>
      </body>
    </html>
  );
}
