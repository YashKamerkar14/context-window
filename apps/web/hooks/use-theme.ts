"use client";

import { useSyncExternalStore } from "react";

export type Theme = "light" | "dark" | "system";

const EVENT = "cw-theme";

function readTheme(): Theme {
  try {
    return (localStorage.getItem("theme") as Theme | null) ?? "system";
  } catch {
    return "system";
  }
}

function subscribeTheme(onChange: () => void): () => void {
  window.addEventListener(EVENT, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}

/** The saved theme choice; "system" on the server and until the browser says otherwise. */
export function useThemeChoice(): Theme {
  return useSyncExternalStore(subscribeTheme, readTheme, () => "system");
}

export function chooseTheme(theme: Theme): void {
  try {
    localStorage.setItem("theme", theme);
  } catch {
    // private mode: the choice lasts for this page only
  }
  const dark =
    theme === "dark" ||
    (theme === "system" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
  window.dispatchEvent(new Event(EVENT));
}

function subscribeDark(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["class"],
  });
  return () => observer.disconnect();
}

/** Whether the page is currently dark (the `dark` class on <html>). */
export function useDarkMode(): boolean {
  return useSyncExternalStore(
    subscribeDark,
    () => document.documentElement.classList.contains("dark"),
    () => false,
  );
}
