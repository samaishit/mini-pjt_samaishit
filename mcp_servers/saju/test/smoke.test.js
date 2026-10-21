// stdio JSON-RPC 라운드트립 회귀 테스트.
// 빌드된 서버(dist/index.js)를 서브프로세스로 띄워 calculate_saju를 호출하고,
// known-good 픽스처(ssaju README 검증 샘플)가 출력에 그대로 나오는지 확인한다.
// 2001-11-03 14:20 남 → 일간 庚 · 인수격 · 용신 己·丙·癸.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SERVER = join(__dirname, "..", "dist", "index.js");

// 서버에 줄단위 JSON-RPC 메시지들을 보내고, 주어진 id들의 응답을 모아 돌려준다.
function rpc(messages, wantIds) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [SERVER], {
      stdio: ["pipe", "pipe", "inherit"],
    });
    const responses = {};
    let buf = "";
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error("timeout waiting for responses"));
    }, 10000);

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

test("calculate_saju returns known-good fixture over stdio", async () => {
  const init = {
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "smoke", version: "0" },
    },
  };
  const initialized = { jsonrpc: "2.0", method: "notifications/initialized" };
  const call = {
    jsonrpc: "2.0",
    id: 2,
    method: "tools/call",
    params: {
      name: "calculate_saju",
      arguments: { year: 2001, month: 11, day: 3, hour: 14, minute: 20, gender: "남" },
    },
  };

  const res = await rpc([init, initialized, call], [1, 2]);

  // initialize 응답에 서버 이름이 들어있는지
  assert.equal(res[1].result.serverInfo.name, "saju-mcp");

  // tools/call 응답 본문 추출
  assert.ok(!res[2].error, `tool error: ${JSON.stringify(res[2].error)}`);
  const text = res[2].result.content[0].text;

  // known-good 픽스처 — ssaju 출력을 래퍼가 그대로 통과시키는지
  assert.match(text, /庚/, "일간 庚 누락");
  assert.match(text, /인수격/, "격 인수격 누락");
  for (const god of ["己", "丙", "癸"]) {
    assert.match(text, new RegExp(god), `용신 ${god} 누락`);
  }
});
