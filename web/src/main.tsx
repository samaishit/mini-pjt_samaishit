import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
// Pico CSS(디자인 시스템) → 서체(Pretendard 본문, Noto Serif KR 제목, 모두 OFL·앱에 함께 빌드) → 서비스 테마
import "@picocss/pico/css/pico.min.css";
import "pretendard/dist/web/variable/pretendardvariable-dynamic-subset.css";
import "@fontsource/noto-serif-kr/600.css";
import "@fontsource/noto-serif-kr/700.css";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
