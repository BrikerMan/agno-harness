import { useCallback, useEffect, useRef } from "react";

/** Re-attach only when the user is actually at the bottom. */
const NEAR_BOTTOM_PX = 24;

export function useStickToBottom() {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);
  const lastTopRef = useRef(0);

  const stick = useCallback(() => {
    const el = scrollerRef.current;
    if (!el || !pinnedRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, []);

  const pin = useCallback(() => {
    pinnedRef.current = true;
    stick();
  }, [stick]);

  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;

    const onScroll = () => {
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      if (distance <= NEAR_BOTTOM_PX) {
        pinnedRef.current = true;
      } else if (el.scrollTop < lastTopRef.current) {
        pinnedRef.current = false;
      }
      lastTopRef.current = el.scrollTop;
    };

    const observer = new ResizeObserver(() => stick());
    const content = el.firstElementChild;
    if (content) observer.observe(content);

    el.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      observer.disconnect();
      el.removeEventListener("scroll", onScroll);
    };
  }, [stick]);

  return { scrollerRef, pin };
}
