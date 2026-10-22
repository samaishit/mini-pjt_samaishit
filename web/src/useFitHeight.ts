import { useEffect, type RefObject } from "react";

/** 이 너비 이하는 패널을 위아래로 쌓으므로 화면 높이에 맞추지 않는다 (styles.css 의 860px 와 같은 값) */
export const STACK_BREAKPOINT = 860;

/**
 * 요소가 화면 아래 끝까지 딱 맞게 높이를 잰다: 실제 시작 위치와 페이지 아래 여백을 빼고 남은 높이를
 * CSS 변수(varName)로 넣는다. 패널은 이 높이를 채우고 넘치는 내용은 패널 안에서 스크롤한다 (페이지 전체 스크롤 없이).
 */
export function useFitHeight(ref: RefObject<HTMLElement | null>, varName: string, minPx = 520) {
  useEffect(() => {
    const box = ref.current;
    if (!box) return;
    const fit = () => {
      if (window.innerWidth <= STACK_BREAKPOINT) {
        box.style.removeProperty(varName);
        return;
      }
      const top = box.getBoundingClientRect().top + window.scrollY;
      const page = box.closest(".page");
      const pad = page ? parseFloat(getComputedStyle(page).paddingBottom) || 0 : 0;   // 페이지 아래 여백까지 빼야 전체 스크롤이 안 생긴다
      if (top > 0) box.style.setProperty(varName, `${Math.max(minPx, Math.floor(window.innerHeight - top - pad))}px`);   // 숨은 탭은 top 이 0
    };
    fit();
    // 탭이 보이게 될 때(크기 변화)도 다시 잰다. ResizeObserver 가 없는 환경(테스트 등)은 창 크기 변화만 본다
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(fit);
    ro?.observe(box);
    window.addEventListener("resize", fit);
    return () => {
      ro?.disconnect();
      window.removeEventListener("resize", fit);
    };
  }, [ref, varName, minPx]);
}
