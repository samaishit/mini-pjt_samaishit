#!/usr/bin/env node
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { calculateSaju, serializeSaju, type SajuInput } from "ssaju";

// [fork] 기준일 문자열 형식. 절기 경계는 그날 23:59 KST로 판정한다.
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

const birthSchema = {
  year: z.number().int().describe("양력/음력 연도 (예: 2001)"),
  month: z.number().int().min(1).max(12).describe("월 1-12"),
  day: z.number().int().min(1).max(31).describe("일 1-31"),
  hour: z.number().int().min(0).max(23).default(12).describe("시 0-23 (기본 12)"),
  minute: z.number().int().min(0).max(59).default(0).describe("분 0-59 (기본 0)"),
  gender: z
    .enum(["남", "여"])
    .optional()
    .describe("성별. 대운 방향에 영향 → 정확한 결과 위해 제공 권장"),
  calendar: z
    .enum(["solar", "lunar"])
    .default("solar")
    .describe("입력 달력 종류 (기본 solar)"),
  leap: z.boolean().default(false).describe("음력 윤달 여부 (calendar=lunar일 때만 의미)"),
  timezone: z.string().default("Asia/Seoul").describe("IANA 시간대 (기본 Asia/Seoul)"),
  longitude: z.number().optional().describe("경도. 진태양시 보정용"),
  applyLocalMeanTime: z
    .boolean()
    .optional()
    .describe("진태양시(지방평균시) 보정 적용 여부"),
};

const inputSchema = {
  ...birthSchema,
  // [fork] 세운·월운·만세력 기준 시각. 생략하면 시스템 시각을 쓴다.
  now: z
    .string()
    .regex(DATE_RE)
    .optional()
    .describe("기준일 YYYY-MM-DD (세운·월운 계산 기준, 그날 23:59 KST). 생략 시 현재 시각"),
  format: z
    .enum(["compact", "markdown", "json"])
    .default("compact")
    .describe(
      "출력 형식. compact=LLM용 압축(기본), markdown=사람용 상세, json=구조화 데이터(함수 필드 제외)",
    ),
};

const server = new McpServer({
  name: "saju-mcp",
  version: "1.0.0",
});

server.registerTool(
  "calculate_saju",
  {
    title: "사주/만세력 계산",
    description:
      "생년월일시로 사주·만세력을 계산한다. 원국(사주팔자)·십성·12운성·합충형파해·신살·격국·용신·대운·세운·월운·공망을 한 번에 산출한다. gender는 대운 방향에 영향을 주므로 정확한 결과를 위해 제공을 권장한다.",
    inputSchema,
  },
  async (args) => {
    const { format, now, ...rest } = args;
    const result = calculateSaju({
      ...rest,
      ...(now ? { now: kstEndOfDay(now) } : {}),
    } as SajuInput);
    const text =
      format === "json"
        ? JSON.stringify(serializeSaju(result), null, 2)
        : format === "markdown"
          ? result.toMarkdown()
          : result.toCompact();
    return {
      content: [{ type: "text", text }],
    };
  }
);

// ---------------------------------------------------------------------------
// [fork] get_luck_calendar: 기준일부터 N개 절기 월의 월운·세운 달력
// ---------------------------------------------------------------------------
// ssaju의 wolun은 `now`가 속한 양력 연도의 寅~丑월만 주고 절입 날짜는 주지 않는다.
// 그래서 하루씩 23:59 KST로 reference.codes.thisMonth/thisYear를 확인해
// 월이 바뀌는 날(절입일)을 찾고, 십성·12운성은 해당 시점의 wolun/seyun에서 가져온다.

const DAY_MS = 86_400_000;

/** YYYY-MM-DD → 그날 23:59 KST(= 14:59 UTC) */
function kstEndOfDay(date: string): Date {
  const [y, m, d] = date.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d, 14, 59));
}

/** kstEndOfDay로 만든 Date → YYYY-MM-DD */
function toDateString(t: Date): string {
  return t.toISOString().slice(0, 10);
}

type Codes = { month: string; year: string };

function codesAt(birth: SajuInput, t: Date): Codes {
  const r = calculateSaju({ ...birth, now: t });
  return { month: r.reference.codes.thisMonth, year: r.reference.codes.thisYear };
}

/** 절기 월 시작일의 십성·12운성. wolun은 양력 연도 단위라 丑월(1월)은 전년도 결과에서 찾는다. */
function monthTenGods(birth: SajuInput, start: Date, monthGanzhi: string, yearGanzhi: string) {
  for (const t of [start, new Date(start.getTime() - 40 * DAY_MS)]) {
    const r = calculateSaju({ ...birth, now: t });
    const w = r.wolun.find((item) => item.ganzhi === monthGanzhi);
    if (!w) continue;
    const s = r.seyun.find((item) => item.ganzhi === yearGanzhi);
    return {
      stem_ten_god: w.stem_tengod,
      branch_ten_god: w.branch_tengod,
      stage12: w["12unsung"],
      year_stem_ten_god: s?.tenGodStem ?? null,
      year_branch_ten_god: s?.tenGodBranch ?? null,
    };
  }
  return {
    stem_ten_god: null,
    branch_ten_god: null,
    stage12: null,
    year_stem_ten_god: null,
    year_branch_ten_god: null,
  };
}

server.registerTool(
  "get_luck_calendar",
  {
    title: "절기 월운 달력",
    description:
      "기준일(from)이 속한 절기 월부터 N개 절기 월의 월운 간지, 세운 간지, 십성, 12운성, 시작일·종료일을 계산한다. 월 경계는 절입일(23:59 KST 판정), 세운은 입춘 기준이다. 월별 여행·이사 시기 판단에 사용한다. 원국·용신이 필요하면 calculate_saju를 쓴다.",
    inputSchema: {
      ...birthSchema,
      from: z.string().regex(DATE_RE).describe("기준일 YYYY-MM-DD. 이 날짜가 속한 절기 월부터 시작"),
      months: z.number().int().min(1).max(24).default(12).describe("절기 월 개수 (기본 12)"),
    },
  },
  async (args) => {
    const { from, months, ...birthArgs } = args;
    const birth = birthArgs as SajuInput;
    const origin = kstEndOfDay(from);
    const originCodes = codesAt(birth, origin);

    // 1) 기준일이 속한 절기 월의 시작일: 월 간지가 바뀌기 직전까지 거꾸로 스캔
    let start = origin;
    for (let i = 0; i < 40; i++) {
      const prev = new Date(start.getTime() - DAY_MS);
      if (codesAt(birth, prev).month !== originCodes.month) break;
      start = prev;
    }

    // 2) 앞으로 스캔하며 월이 바뀌는 날을 경계로 기록
    const result = [];
    let cur = { start, codes: codesAt(birth, start) };
    let t = start;
    while (result.length < months) {
      t = new Date(t.getTime() + DAY_MS);
      const c = codesAt(birth, t);
      if (c.month === cur.codes.month) continue;
      result.push({
        index: result.length + 1,
        month_ganzhi: cur.codes.month,
        year_ganzhi: cur.codes.year,
        start_date: toDateString(cur.start),
        end_date: toDateString(new Date(t.getTime() - DAY_MS)),
        ...monthTenGods(birth, cur.start, cur.codes.month, cur.codes.year),
      });
      cur = { start: t, codes: c };
    }

    const payload = { reference_date: from, months: result };
    return { content: [{ type: "text", text: JSON.stringify(payload, null, 2) }] };
  }
);

const transport = new StdioServerTransport();
await server.connect(transport);
