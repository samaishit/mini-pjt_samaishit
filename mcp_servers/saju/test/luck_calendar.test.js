// [fork] get_luck_calendar / calculate_saju(now) stdio 회귀 테스트.
// 기대값은 ssaju를 하루 단위(23:59 KST)로 스캔해 확인한 2026-10-21 기준 12개 절기 월 표다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SERVER = join(__dirname, "..", "dist", "index.js");

// 교육용 가상 프로필: 1992-04-12 14:30 여 (戊午일, 용신 丙·癸·甲)
const BIRTH = { year: 1992, month: 4, day: 12, hour: 14, minute: 30, gender: "여" };

function rpc(messages, wantIds) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [SERVER], { stdio: ["pipe", "pipe", "inherit"] });
    const responses = {};
    let buf = "";
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error("timeout waiting for responses"));
    }, 20000);
    child.stdout.on("data", (chunk) => {
      buf += chunk.toString();
      let nl;
      while ((nl = buf.indexOf("\n")) !== -1) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        const msg = JSON.parse(line);
        if (msg.id !== undefined) responses[msg.id] = msg;
        if (wantIds.every((id) => responses[id] !== undefined)) {
          clearTimeout(timer);
          child.kill();
          resolve(responses);
        }
      }
    });
    child.on("error", reject);
    for (const m of messages) child.stdin.write(JSON.stringify(m) + "\n");
  });
}

async function callTool(name, args) {
  const init = {
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "test", version: "0" } },
  };
  const initialized = { jsonrpc: "2.0", method: "notifications/initialized" };
  const call = { jsonrpc: "2.0", id: 2, method: "tools/call", params: { name, arguments: args } };
  const res = await rpc([init, initialized, call], [1, 2]);
  assert.ok(!res[2].error, `tool error: ${JSON.stringify(res[2].error)}`);
  assert.ok(!res[2].result.isError, `tool isError: ${res[2].result.content?.[0]?.text}`);
  return res[2].result.content[0].text;
}

test("get_luck_calendar reproduces the verified 12-month table for 2026-10-21", async () => {
  const data = JSON.parse(await callTool("get_luck_calendar", { ...BIRTH, from: "2026-10-21" }));
  const expected = [
    ["戊戌", "2026-10-08", "丙午"], ["己亥", "2026-11-07", "丙午"], ["庚子", "2026-12-07", "丙午"],
    ["辛丑", "2027-01-05", "丙午"], ["壬寅", "2027-02-04", "丁未"], ["癸卯", "2027-03-06", "丁未"],
    ["甲辰", "2027-04-05", "丁未"], ["乙巳", "2027-05-06", "丁未"], ["丙午", "2027-06-06", "丁未"],
    ["丁未", "2027-07-07", "丁未"], ["戊申", "2027-08-08", "丁未"], ["己酉", "2027-09-08", "丁未"],
  ];
  assert.equal(data.reference_date, "2026-10-21");
  assert.equal(data.months.length, 12);
  data.months.forEach((m, i) => {
    const [ganzhi, start, year] = expected[i];
    assert.equal(m.month_ganzhi, ganzhi, `#${i + 1} 월운`);
    assert.equal(m.start_date, start, `#${i + 1} 시작일`);
    assert.equal(m.year_ganzhi, year, `#${i + 1} 세운`);
    assert.ok(m.stem_ten_god && m.branch_ten_god && m.stage12, `#${i + 1} 십성·12운성 누락`);
  });
  assert.equal(data.months[0].end_date, "2026-11-06");
  assert.equal(data.months[11].end_date, "2027-10-07");
});

test("get_luck_calendar starts at 己亥 on 입동 day (23:59 KST rule)", async () => {
  const data = JSON.parse(await callTool("get_luck_calendar", { ...BIRTH, from: "2026-11-07", months: 1 }));
  assert.equal(data.months[0].month_ganzhi, "己亥");
  assert.equal(data.months[0].start_date, "2026-11-07");
});

test("get_luck_calendar keeps 丙午 year before 입춘 when starting in January", async () => {
  const data = JSON.parse(await callTool("get_luck_calendar", { ...BIRTH, from: "2027-01-15", months: 2 }));
  assert.deepEqual(
    data.months.map((m) => [m.month_ganzhi, m.start_date, m.year_ganzhi]),
    [["辛丑", "2027-01-05", "丙午"], ["壬寅", "2027-02-04", "丁未"]],
  );
});

test("calculate_saju honors the now parameter", async () => {
  const text = await callTool("calculate_saju", { ...BIRTH, now: "2026-10-21", format: "json" });
  const data = JSON.parse(text);
  assert.equal(data.reference.codes.thisMonth, "戊戌");
  assert.equal(data.reference.codes.thisYear, "丙午");
});
