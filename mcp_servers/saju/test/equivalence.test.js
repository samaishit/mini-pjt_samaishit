// format:"json" 출력 == 엔진 직접 import(serializeSaju) 출력 검증.
// 두 경로가 동일한 공유 직렬화를 쓰는지 stdio 라운드트립으로 확인한다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { calculateSaju, serializeSaju } from "ssaju";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SERVER = join(__dirname, "..", "dist", "index.js");

function rpc(messages, wantIds) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [SERVER], { stdio: ["pipe", "pipe", "inherit"] });
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

const INPUT = { year: 2001, month: 11, day: 3, hour: 14, minute: 20, gender: "남" };

test("format:json MCP output equals direct serializeSaju import", async () => {
  const init = {
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "eq", version: "0" } },
  };
  const initialized = { jsonrpc: "2.0", method: "notifications/initialized" };
  const call = {
    jsonrpc: "2.0",
    id: 2,
    method: "tools/call",
    params: { name: "calculate_saju", arguments: { ...INPUT, format: "json" } },
  };

  const res = await rpc([init, initialized, call], [1, 2]);
  assert.ok(!res[2].error, `tool error: ${JSON.stringify(res[2].error)}`);

  const mcpData = JSON.parse(res[2].result.content[0].text);
  const engineData = JSON.parse(JSON.stringify(serializeSaju(calculateSaju(INPUT))));

  // reference.now 는 호출 순간 타임스탬프라 두 경로에서 ms 단위로 다를 수 있음 → 비교 제외.
  delete mcpData.reference.now;
  delete engineData.reference.now;

  assert.deepStrictEqual(mcpData, engineData, "MCP json output should equal engine serialize output");
});
