import type { BirthFields } from "../composer";

interface Props {
  value: BirthFields;
  onChange: (next: BirthFields) => void;
}

/** 출생 정보 입력 폼. 값은 화면 상태에만 두고 브라우저 저장소에 남기지 않는다. */
export function BirthForm({ value: b, onChange }: Props) {
  const set = <K extends keyof BirthFields>(key: K, v: BirthFields[K]) => onChange({ ...b, [key]: v });
  const num = (s: string) => (s === "" ? NaN : Number(s));
  const time = `${String(b.hour).padStart(2, "0")}:${String(b.minute).padStart(2, "0")}`;

  return (
    <fieldset className="birth">
      <legend>출생 정보</legend>
      <div className="row">
        <label className="grow">
          연
          <input type="number" inputMode="numeric" min={1900} max={2100} value={Number.isNaN(b.year) ? "" : b.year}
                 onChange={(e) => set("year", num(e.target.value))} aria-label="출생 연도" />
        </label>
        <label>
          월
          <input type="number" inputMode="numeric" min={1} max={12} value={Number.isNaN(b.month) ? "" : b.month}
                 onChange={(e) => set("month", num(e.target.value))} aria-label="출생 월" />
        </label>
        <label>
          일
          <input type="number" inputMode="numeric" min={1} max={31} value={Number.isNaN(b.day) ? "" : b.day}
                 onChange={(e) => set("day", num(e.target.value))} aria-label="출생 일" />
        </label>
      </div>
      <div className="row">
        <div className="seg" role="radiogroup" aria-label="양력/음력">
          {(["solar", "lunar"] as const).map((c) => (
            <button key={c} type="button" role="radio" aria-checked={b.calendar === c}
                    className={b.calendar === c ? "on" : ""} onClick={() => set("calendar", c)}>
              {c === "solar" ? "양력" : "음력"}
            </button>
          ))}
        </div>
        <label className="check">
          <input type="checkbox" checked={b.calendar === "lunar" && b.leap} disabled={b.calendar !== "lunar"}
                 onChange={(e) => set("leap", e.target.checked)} />
          윤달
        </label>
        <div className="seg" role="radiogroup" aria-label="성별">
          {(["남", "여"] as const).map((g) => (
            <button key={g} type="button" role="radio" aria-checked={b.gender === g}
                    className={b.gender === g ? "on" : ""} onClick={() => set("gender", g)}>
              {g === "남" ? "남성" : "여성"}
            </button>
          ))}
        </div>
      </div>
      <div className="row">
        <label className="time">
          출생 시각
          <input type="time" value={time} disabled={!b.timeKnown} aria-label="출생 시각"
                 onChange={(e) => {
                   const [h, m] = e.target.value.split(":").map(Number);
                   if (!Number.isNaN(h)) onChange({ ...b, hour: h, minute: m || 0 });
                 }} />
        </label>
        <label className="check">
          <input type="checkbox" checked={!b.timeKnown} onChange={(e) => set("timeKnown", !e.target.checked)} />
          시각 모름
        </label>
      </div>
    </fieldset>
  );
}
