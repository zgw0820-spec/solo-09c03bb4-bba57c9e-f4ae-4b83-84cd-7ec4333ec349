// 前端单调合并逻辑的确定性测试（Node vm 沙箱 + DOM/fetch 桩）。
// 验证迟到响应不能让已显示的盘点、方案列表、复核详情倒退。
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.join(here, "..", "app", "templates", "index.html"), "utf8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1]
  .replace(/{{\s*max_groups\s*}}/g, "12")
  // 暴露页面内部状态与函数给测试（vm 顶层 let 不会自动成为 context 属性）
  + "\nglobalThis.__page = { get snapshot(){return snapshot;}, get plans(){return plans;},"
  + " get shownReview(){return shownReview;}, doScan, load, doPlan, doReview, applyResponse };";


class El {
  constructor() {
    this.style = {};
    this.dataset = {};
    this.listeners = {};
    this._html = "";
    this._text = "";
    this.value = "";
  }
  addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); }
  querySelectorAll() { return []; }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = v; }
  get textContent() { return this._text; }
  set textContent(v) { this._text = v; }
}

const pending = [];
const els = new Map();
function getEl(key) {
  if (!els.has(key)) els.set(key, new El());
  return els.get(key);
}
const sandbox = {
  console,
  setTimeout: (fn) => 0,
  clearTimeout: () => {},
  alert: (m) => { sandbox.__alerts.push(m); },
  document: {
    getElementById: (id) => getEl("#" + id),
    querySelector: (sel) => getEl(sel),
  },
  fetch: () => new Promise((resolve, reject) => pending.push({ resolve, reject })),
};
sandbox.__alerts = [];
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(script, sandbox);
const S = sandbox.__page;

let failures = 0;
function check(name, cond, detail) {
  console.log(`[${cond ? "PASS" : "FAIL"}] ${name}${!cond && detail ? " — " + detail : ""}`);
  if (!cond) failures++;
}
function flushMicro() { return new Promise((r) => setTimeout(r, 0)); }
function resolveNext(payload, ok = true) {
  const p = pending.shift();
  if (!p) throw new Error("没有等待中的请求");
  p.resolve({ ok, json: async () => payload });
}
function snap(seq) {
  return {
    id: `s${seq}`, seq, taken_at: "t", volume: { used: seq },
    files: [{ key: `k${seq}`, path: `/f${seq}`, bytes: seq, holders: [seq] }],
    groups: { [seq]: { pgid: seq, pids: [seq], names: ["x"], cmdline: "x" } },
  };
}
function plan(id, reviewSeq = null, valid = true) {
  return {
    id, created_at: "t", groups: [1], bytes: 1, target_bytes: 1,
    review: reviewSeq == null ? null : { seq: reviewSeq, valid, plan_id: id, invalidations: [], notices: [] },
  };
}

// ---- 初始 load：seq2 的盘点 + 方案 p1（未复核） ----
resolveNext({ snapshot: snap(2), plans: [plan("p1")] });
await flushMicro();
check("初始加载显示 seq2 盘点", S.snapshot.seq === 2);
check("初始加载显示方案 p1", S.plans.some((p) => p.id === "p1"));

// ---- 场景 A：更早的盘点(seq1)迟到，且响应里的方案列表缺 p1 ----
S.doScan();
await flushMicro();
resolveNext({ snapshot: snap(1), plans: [] }); // 故意给出更旧、更不全的列表
await flushMicro();
check("A1 迟到的旧盘点不覆盖当前盘点（仍 seq2）", S.snapshot.seq === 2);
check("A2 迟到响应缺少的方案不被删除（p1 仍在）", S.plans.some((p) => p.id === "p1"));
check("A3 旧响应到达时显示过期提示",
  els.get("#stale-note").style.display === "block" && els.get("#stale-note").textContent !== "");

// ---- 场景 B：更新的盘点(seq3)正常生效 ----
S.doScan();
await flushMicro();
resolveNext({ snapshot: snap(3), plans: [plan("p1")] });
await flushMicro();
check("B 更新的盘点 seq3 生效", S.snapshot.seq === 3);

// ---- 场景 C：同一方案两次复核乱序，先开始(seq4)的响应迟到 ----
S.doReview("p1"); // seq4 先开始
await flushMicro();
S.doReview("p1"); // seq5 后开始
await flushMicro();
resolveNext({ review: { ...plan("p1", 5).review, plan_id: "p1" }, snapshot: snap(5), plans: [plan("p1", 5, false)] });
await flushMicro();
check("C1 后开始的复核(seq5)先到 → 面板显示 seq5", S.shownReview.seq === 5);
check("C2 方案列表中 p1 复核为 seq5", S.plans.find((p) => p.id === "p1").review.seq === 5);
check("C3 当前盘点更新为 seq5", S.snapshot.seq === 5);
resolveNext({ review: { ...plan("p1", 4).review, plan_id: "p1" }, snapshot: snap(4), plans: [plan("p1", 4, true)] });
await flushMicro();
check("C4 较早复核(seq4)迟到 → 面板不倒退（仍 seq5）", S.shownReview.seq === 5);
check("C5 方案列表复核结论不倒退（仍 seq5/作废）",
  S.plans.find((p) => p.id === "p1").review.seq === 5 &&
  S.plans.find((p) => p.id === "p1").review.valid === false);
check("C6 当前盘点不倒退回 seq4", S.snapshot.seq === 5);

// ---- 场景 D：另一方案 p2 的复核正常显示，不影响 p1 ----
S.applyResponse({ plans: [plan("p1", 5, false), plan("p2")] });
S.doReview("p2");
await flushMicro();
resolveNext({ review: { seq: 6, valid: true, plan_id: "p2" }, snapshot: snap(6),
              plans: [plan("p1", 5, false), plan("p2", 6, true)] });
await flushMicro();
check("D1 切换方案复核 → 面板显示 p2 的 seq6",
  S.shownReview.plan_id === "p2" && S.shownReview.seq === 6);
check("D2 p1 的复核结论保持 seq5 不受影响",
  S.plans.find((p) => p.id === "p1").review.seq === 5);
check("D3 当前盘点更新为 seq6", S.snapshot.seq === 6);

// ---- 场景 E：p2 的旧复核(seq6 重复/更小 seq)迟到不倒退 ----
S.doReview("p2"); // seq7
await flushMicro();
resolveNext({ review: { seq: 7, valid: false, plan_id: "p2" }, snapshot: snap(7),
              plans: [plan("p1", 5, false), plan("p2", 7, false)] });
await flushMicro();
// 再来一个迟到的 seq6 响应
S.applyResponse({ snapshot: snap(6), plans: [plan("p2", 6, true)] });
check("E1 旧盘点 seq6 不覆盖 seq7", S.snapshot.seq === 7);
check("E2 p2 复核不倒退回 seq6",
  S.plans.find((p) => p.id === "p2").review.seq === 7);
check("E3 p1 方案仍保留（未被不含它的响应冲掉）",
  S.plans.some((p) => p.id === "p1"));

// ---- 场景 F：生成方案响应的列表偏旧时，新方案不丢失 ----
// 模拟一次成功的 /api/plan 响应（含此前没有的新方案 p3）
S.applyResponse({ plans: [plan("p3", null), plan("p2", 7, false), plan("p1", 5, false)] });
check("F1 新方案 p3 并入列表", S.plans.some((p) => p.id === "p3"));
S.applyResponse({ plans: [plan("p1", 5, false)] }); // 迟到且不含 p2/p3
check("F2 迟到响应不能删掉 p2/p3",
  S.plans.some((p) => p.id === "p2") && S.plans.some((p) => p.id === "p3"));
check("F3 方案按编号倒序排列",
  S.plans.map((p) => p.id).join(",") === "p3,p2,p1");

console.log(failures ? `\n${failures} 项前端测试失败` : "\n全部前端单调合并测试通过。");
process.exit(failures ? 1 : 0);
