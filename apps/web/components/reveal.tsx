"use client";

import { type ReactNode, useEffect, useRef, useState } from "react";

/** True once the element has scrolled into view (and stays true). */
function useSeen<T extends Element>() {
  const ref = useRef<T>(null);
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setSeen(true);
          observer.disconnect();
        }
      },
      { rootMargin: "0px 0px -12% 0px" },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, seen] as const;
}

/** Fades its children up the first time they scroll into view. */
export function Reveal({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  const [ref, seen] = useSeen<HTMLDivElement>();
  return (
    <div
      ref={ref}
      className={`${seen ? "cw-reveal-in" : "cw-reveal"} ${className}`}
    >
      {children}
    </div>
  );
}

/** A number that counts up from zero the first time it scrolls into view. */
export function CountUp({ value }: { value: number }) {
  const [ref, seen] = useSeen<HTMLSpanElement>();
  const [shown, setShown] = useState(value);
  useEffect(() => {
    if (!seen || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const start = performance.now();
    let frame = 0;
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / 1200);
      setShown(Math.round(value * (1 - (1 - t) ** 3)));
      if (t < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [seen, value]);
  return (
    <span ref={ref} className="tabular-nums">
      {shown.toLocaleString("en-GB")}
    </span>
  );
}

/** The fact desk's "verified" stamp, which lands when the story scrolls into view. */
export function Stamp({
  supported,
  total,
  large = false,
}: {
  supported: number;
  total: number;
  large?: boolean;
}) {
  const [ref, seen] = useSeen<HTMLDivElement>();
  return (
    <div
      ref={ref}
      title={`The fact desk checked ${total} ${total === 1 ? "claim" : "claims"} against their sources`}
      className={`${seen ? "cw-stamp-in" : "cw-stamp"} grid shrink-0 place-items-center rounded-full border-2 border-double border-emerald-600 text-center font-semibold uppercase leading-tight text-emerald-700 dark:border-emerald-400 dark:text-emerald-300 ${
        large ? "size-24 text-[11px]" : "size-[76px] text-[9px]"
      }`}
    >
      <span>
        <span className={large ? "text-2xl" : "text-base"} aria-hidden>
          ✓
        </span>
        <br />
        Verified
        <br />
        <span className="font-normal normal-case tabular-nums">
          {supported}/{total} claims
        </span>
      </span>
    </div>
  );
}
