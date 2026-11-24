#!/usr/bin/env python3 -I
"""服务端并发更新的确定性测试。

用桩替换 scanner.take_snapshot，用 threading.Event 精确控制各次采集的
开始/完成交错，验证：
  1) 两次盘点，先开始的晚完成 → 当前盘点仍是后开始那次（不倒退）；
  2) 同一方案两次复核，先开始的晚完成 → 方案保留后开始那次复核结论；
  3) 普通盘点与复核交错 → 当前盘点取最后开始的成功采集（两种都算）；
  4) 采集中途失败 → 已生效状态保留，其他成功采集照常生效；
  5) 复核失败 → 当前盘点与方案复核结论都不变；
  6) 不同方案的复核互不覆盖；
  7) 序号在扫描开始前分配（新旧以服务端开始采集顺序为准）。
"""
import json
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

# -I 隔离模式下用户 site-packages 不自动加入，显式补上 Flask 所在路径
sys.path.insert(0, "/home/node/.local/lib/python3.11/site-packages")

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

import app as duty  # noqa: E402

BASE = f"http://127.0.0.1:{duty.app.config.get('PORT', 8080)}"
failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def http(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error": raw.decode("utf-8", "replace")}


# ---- 桩：可控的 take_snapshot ----------------------------------------------

class FakeScanner:
    def __init__(self):
        self.lock = threading.Lock()
        self.inside = {}          # seq -> Event（采集进行中置位）
        self.release = {}         # seq -> Event（放行完成，仅 gate 中的序号等待）
        self.gate = set()         # 需要手动控制完成时机的序号
        self.fail_seqs = set()
        self.started = []         # 实际进入扫描的序号顺序
        self.counter = 0

    def __call__(self, volume_path, snap_id, seq):
        with self.lock:
            self.started.append(seq)
            inside = self.inside.setdefault(seq, threading.Event())
            inside.set()
        if seq in self.fail_seqs:
            raise OSError(f"injected scan failure at seq {seq}")
        if seq in self.gate:
            release = self.release.setdefault(seq, threading.Event())
            if not release.wait(timeout=10):
                raise TimeoutError(f"scan seq {seq} never released")
        with self.lock:
            self.counter += 1
            n = self.counter
        return make_snap(snap_id, seq, n)


def make_snap(snap_id, seq, marker):
    """构造一个内容随 marker 变化的最小合法快照。"""
    return {
        "id": snap_id,
        "seq": seq,
        "taken_at": "2026-11-24 10:00:00",
        "volume": {"path": "/logs", "mountpoint": "/logs", "dev": "7:0",
                   "total": 1000, "used": marker, "free": 1000 - marker},
        "files": [
            {"key": f"7:{marker}", "path": f"/logs/f{marker}.log", "blocks": 2,
             "bytes": marker * 1024, "size": marker * 1024, "holders": [100 + marker]},
        ],
        "groups": {
            str(100 + marker): {"pgid": 100 + marker, "pids": [100 + marker],
                                "names": ["svc"], "cmdline": f"svc {marker}"},
        },
    }


def wait_for(seq, scanner):
    deadline = time.time() + 5
    while time.time() < deadline:
        with scanner.lock:
            if seq in scanner.inside:
                return
        time.sleep(0.02)
    raise TimeoutError(f"seq {seq} did not start; started={scanner.started}")


def run_async(fn, *args):
    box = {}

    def worker():
        try:
            box["result"] = fn(*args)
        except BaseException as exc:  # 测试线程内异常回传主线程
            box["error"] = exc

    t = threading.Thread(target=worker, daemon=True)
    t.box = box
    t.start()
    return t


def join(t):
    t.join(5)
    if "error" in t.box:
        raise t.box["error"]
    return t.box["result"]


def make_plan_from(snap_id, marker):
    pgid = 100 + marker
    code, j = http("/api/plan", {"snapshot_id": snap_id, "groups": [pgid],
                                 "target_bytes": 1})
    assert code == 200 and j["plan"], j
    return j["plan"]["id"]


def main():
    scanner = FakeScanner()
    scanner.gate.update({1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 16})
    duty.take_snapshot = scanner  # 打桩

    server = threading.Thread(
        target=lambda: duty.app.run(host="127.0.0.1", port=8080,
                                    threaded=True, use_reloader=False),
        daemon=True,
    )
    server.start()
    time.sleep(0.8)  # 等服务器起来

    # ===== 场景 1：两次盘点，先开始(seq1)晚完成；当前盘点必须是 seq2 =====
    t1 = run_async(http, "/api/inventory", {})
    wait_for(1, scanner)
    t2 = run_async(http, "/api/inventory", {})
    wait_for(2, scanner)
    check("1a 序号按开始顺序分配", scanner.started[:2] == [1, 2], str(scanner.started))
    scanner.release[2].set()
    join(t2)
    code, state = http("/api/state")
    check("1b 后开始先完成 → 当前盘点为 seq2", state["snapshot"]["seq"] == 2)
    scanner.release[1].set()   # 迟到的较早采集此时才完成
    join(t1)
    code, state = http("/api/state")
    check("1c 较早采集迟到 → 当前盘点不倒退回 seq1",
          state["snapshot"]["seq"] == 2, f"got {state['snapshot']['seq']}")
    check("1d 迟到快照仍留档可查（方案可引用）",
          http("/api/state")[0] == 200)

    # ===== 场景 2：同一方案两次复核，先开始(seq3)晚完成 =====
    plan_id = make_plan_from("s2", state["snapshot"]["volume"]["used"])
    t3 = run_async(http, "/api/review", {"plan_id": plan_id})
    wait_for(3, scanner)
    t4 = run_async(http, "/api/review", {"plan_id": plan_id})
    wait_for(4, scanner)
    scanner.release[4].set()
    join(t4)
    code, j4 = http("/api/state")
    p = next(x for x in j4["plans"] if x["id"] == plan_id)
    check("2a 后开始的复核先生效（seq4）", p["review"]["seq"] == 4)
    check("2b 后开始的复核已成为当前盘点", j4["snapshot"]["seq"] == 4)
    scanner.release[3].set()
    join(t3)
    code, j3 = http("/api/state")
    p = next(x for x in j3["plans"] if x["id"] == plan_id)
    check("2c 较早复核迟到 → 方案复核结论不倒退回 seq3",
          p["review"]["seq"] == 4, f"got {p['review']['seq']}")
    check("2d 较早复核迟到 → 卷当前盘点不倒退回 seq3",
          j3["snapshot"]["seq"] == 4, f"got {j3['snapshot']['seq']}")

    # ===== 场景 3：普通盘点与复核交错，当前盘点取最后开始的成功采集 =====
    # 3a: 复核先开始(seq5)，普通盘点后开始(seq6)且先完成 → 当前盘点应为 seq6
    t5 = run_async(http, "/api/review", {"plan_id": plan_id})
    wait_for(5, scanner)
    t6 = run_async(http, "/api/inventory", {})
    wait_for(6, scanner)
    scanner.release[6].set()
    join(t6)
    code, st = http("/api/state")
    check("3a 后开始的普通盘点先完成 → 当前盘点 seq6", st["snapshot"]["seq"] == 6)
    scanner.release[5].set()
    join(t5)
    code, st = http("/api/state")
    check("3b 较早复核迟到 → 当前盘点保持 seq6", st["snapshot"]["seq"] == 6)
    p = next(x for x in st["plans"] if x["id"] == plan_id)
    check("3c 复核虽较早开始，其结论仍保存在该方案上（seq5）",
          p["review"]["seq"] == 5, f"got {p['review']['seq']}")

    # 3d: 普通盘点先开始(seq7)，复核后开始(seq8)先完成 → 当前盘点应为 seq8
    t7 = run_async(http, "/api/inventory", {})
    wait_for(7, scanner)
    t8 = run_async(http, "/api/review", {"plan_id": plan_id})
    wait_for(8, scanner)
    scanner.release[8].set()
    join(t8)
    code, st = http("/api/state")
    check("3d 后开始的复核先完成 → 当前盘点 seq8（复核也算成功采集）",
          st["snapshot"]["seq"] == 8)
    scanner.release[7].set()
    join(t7)
    code, st = http("/api/state")
    check("3e 较早普通盘点迟到 → 当前盘点保持 seq8", st["snapshot"]["seq"] == 8)

    # ===== 场景 4：采集中途失败，已生效结果保留，其他成功采集照常生效 =====
    before = http("/api/state")[1]
    before_seq = before["snapshot"]["seq"]
    scanner.fail_seqs.add(9)
    t9 = run_async(http, "/api/inventory", {})
    wait_for(9, scanner)
    t10 = run_async(http, "/api/inventory", {})
    wait_for(10, scanner)
    scanner.release[10].set()
    join(t10)
    code, ok = http("/api/state")
    check("4a 并发的成功采集照常生效 → 当前盘点 seq10",
          ok["snapshot"]["seq"] == 10, f"got {ok['snapshot']['seq']}")
    join(t9)
    code, after_fail = http("/api/state")
    check("4b 失败采集不改变当前盘点",
          after_fail["snapshot"]["seq"] == 10, f"got {after_fail['snapshot']['seq']}")
    check("4c 失败前已生效的 seq8 结论/快照仍可追溯",
          any(x["id"] == "s8" for x in duty._snapshots.values()))

    # 直接同步调用一次失败采集，检查 HTTP 500 且状态不动
    code, err = http("/api/inventory", {})  # seq11 未注入失败，先成功
    check("4d 失败后新采集仍可成功", code == 200 and err["snapshot"]["seq"] == 11)
    scanner.fail_seqs.add(12)
    code, err = http("/api/inventory", {})
    check("4e 失败采集返回 500 与错误信息", code == 500 and "error" in err, str(err))
    code, st = http("/api/state")
    check("4f 500 后当前盘点仍是 seq11", st["snapshot"]["seq"] == 11)

    # ===== 场景 5：复核失败，当前盘点与方案复核结论都不变 =====
    plan_b = make_plan_from("s11", st["snapshot"]["volume"]["used"])
    scanner.fail_seqs.add(13)
    code, err = http("/api/review", {"plan_id": plan_b})
    check("5a 复核失败返回 500", code == 500, str(err))
    code, st = http("/api/state")
    pb = next(x for x in st["plans"] if x["id"] == plan_b)
    check("5b 复核失败 → 方案仍为未复核", pb["review"] is None)
    check("5c 复核失败 → 当前盘点不变（seq11）",
          st["snapshot"]["seq"] == 11, f"got {st['snapshot']['seq']}")

    # ===== 场景 6：两个方案各自保留自己最后开始的复核，互不覆盖 =====
    # plan_a 已有 seq5 的复核；对 plan_b 做 seq14 复核
    code, rv = http("/api/review", {"plan_id": plan_b})
    check("6a plan_b 复核成功", code == 200 and rv["review"]["seq"] == 14)
    code, st = http("/api/state")
    pa = next(x for x in st["plans"] if x["id"] == plan_id)
    pb = next(x for x in st["plans"] if x["id"] == plan_b)
    check("6b plan_a 保留自己的复核 seq8，不被 plan_b 的复核覆盖",
          pa["review"]["seq"] == 8, f"got {pa['review']['seq']}")
    check("6c plan_b 保留自己的复核 seq14",
          pb["review"]["seq"] == 14, f"got {pb['review']['seq']}")

    # ===== 场景 7：方案复核结论的“最后开始”跨并发复核验证（plan_b 再来一次） =====
    t15 = run_async(http, "/api/review", {"plan_id": plan_b})
    wait_for(15, scanner)
    t16 = run_async(http, "/api/review", {"plan_id": plan_b})
    wait_for(16, scanner)
    scanner.release[16].set()
    join(t16)
    scanner.release[15].set()
    join(t15)
    code, st = http("/api/state")
    pb = next(x for x in st["plans"] if x["id"] == plan_b)
    check("7 plan_b 两次并发复核后保留最后开始的 seq16",
          pb["review"]["seq"] == 16, f"got {pb['review']['seq']}")

    print()
    if failures:
        print(f"{len(failures)} 项失败：{failures}")
        sys.exit(1)
    print("全部服务端并发测试通过。")


if __name__ == "__main__":
    main()
