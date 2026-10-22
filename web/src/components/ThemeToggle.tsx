import { useEffect, useState } from "react";

type Theme = "auto" | "light" | "dark";
const ORDER: Theme[] = ["auto", "light", "dark"];
const LABEL: Record<Theme, string> = { auto: "자동", light: "낮", dark: "밤" };
const KEY = "saju-theme";

function load(): Theme {
  try {
    const v = window.localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "auto";
  } catch {
    return "auto";
  }
}

/** 낮(한지) · 밤(밤하늘) · 자동(시스템 설정) 전환. 선택은 이 브라우저에만 기억한다. */
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(load);

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "auto") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    try {
      window.localStorage.setItem(KEY, theme);
    } catch {
      // 저장소를 쓸 수 없어도 화면 전환은 된다
    }
  }, [theme]);

  const next = ORDER[(ORDER.indexOf(theme) + 1) % ORDER.length];
  return (
    <button type="button" className="ghost theme-toggle" onClick={() => setTheme(next)}
            aria-label={`화면 테마: ${LABEL[theme]} (눌러서 ${LABEL[next]}으로)`} title="화면 테마 바꾸기">
      <span aria-hidden>{theme === "dark" ? "☾" : theme === "light" ? "☀" : "◐"}</span>
      <span className="theme-label"> {LABEL[theme]}</span>
    </button>
  );
}
