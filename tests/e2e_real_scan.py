#!/usr/bin/env python3 -I
"""端到端测试：真实 sample_holder 进程 + 真实 /proc 扫描 + 真实 HTTP 服务。

覆盖：
- 网页与盘点/方案/复核主流程（真实扫描，非桩）；
- 并发盘点/复核混合请求下，服务端状态单调不倒退（含注入扫描抖动制造乱序完成）；
- 复核能真实发现文件占用变化（持续增长的 worker.log）；
- 采集失败（LOG_VOLUME 不可访问）返回 500 且已生效状态不变。
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, "/home/node/.local/lib/python3.11/site-packages")
APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

VOL = tempfile.mkdtemp(prefix="duty-e2e-")
os.environ["LOG_VOLUME"] = VOL
os.environ["PORT"] = "8091"

import app as duty  # noqa: E402

BASE = "http://127.0.0.1:8091"
failures = []
holders = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def http(path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def start_holder(name, size_mb, extra=()):
    p = subprocess.Popen(
        [sys.executable, "-I", str(APP_DIR / "sample_holder.py"),
         "--file", f"{VOL}/{name}", "--size-mb", str(size_mb), "--settle", "1", *extra],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    holders.append(p)


def wait_inventory_files(expected, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, j = http("/api/inventory", {})
        if code == 200 and len(j["snapshot"]["files"]) >= expected:
            return j
        time.sleep(0.5)
    raise TimeoutError(f"等不到 {expected} 个已删除打开文件")


def main():
    # 1) 起样例进程：api 独占、worker 持续增长、shared 双组持有、cache 独占
    start_holder("api.log", 20)
    start_holder("worker.log", 10, ("--grow-kb-per-sec", "2048"))
    start_holder("shared.log", 15)
    start_holder("shared.log", 15)
    start_holder("cache.log", 5)

    try:
        run_checks()
    finally:
        for p in holders:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        shutil.rmtree(VOL, ignore_errors=True)
    sys.exit(1 if failures else 0)


def run_checks():
    server = threading.Thread(
        target=lambda: duty.app.run(host="127.0.0.1", port=8091,
                                    threaded=True, use_reloader=False),
        daemon=True)
    server.start()
    time.sleep(0.8)

    # 2) 网页可打开，且包含新的过期提示元素
    with urllib.request.urlopen(BASE + "/", timeout=10) as resp:
        page = resp.read().decode()
    check("1 GET / 返回网页", resp.status == 200 and "日志卷回收值班台" in page)
    check("2 网页包含过期提示位与单调合并脚本",
          'id="stale-note"' in page and "applyResponse" in page)

    j = wait_inventory_files(4)
    base_seq = j["snapshot"]["seq"]
    paths = {f["path"] for f in j["snapshot"]["files"]}
    check("3 真实盘点发现 4 个样例文件",
          {"api.log", "worker.log", "shared.log", "cache.log"} <=
          {os.path.basename(p) for p in paths}, str(paths))

    # 3) 并发 6 次盘点，完成过程中轮询 /api/state，当前盘点序号必须单调不减
    seen_seqs = []

    def poller(stop):
        while not stop.is_set():
            code, st = http("/api/state")
            seen_seqs.append(st["snapshot"]["seq"])
            time.sleep(0.01)

    stop = threading.Event()
    pt = threading.Thread(target=poller, args=(stop,))
    pt.start()
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda _: http("/api/inventory", {}), range(6)))
    stop.set()
    pt.join()
    seqs = [r[1]["snapshot"]["seq"] for r in results]
    check("4 并发盘点返回序号互不重复且连续",
          sorted(seqs) == list(range(base_seq + 1, base_seq + 7)), str(sorted(seqs)))
    check("5 轮询期间当前盘点序号单调不减",
          all(b >= a for a, b in zip(seen_seqs, seen_seqs[1:])), str(seen_seqs))
    code, st = http("/api/state")
    check("6 全部成功后当前盘点取最后开始的那次",
          st["snapshot"]["seq"] == max(seqs), f"{st['snapshot']['seq']} vs {max(seqs)}")

    # 4) 真实生成方案：勾选 api.log 持有组
    snap = st["snapshot"]
    api_file = next(f for f in snap["files"] if f["path"].endswith("api.log"))
    api_group = api_file["holders"][0]
    code, pj = http("/api/plan", {"snapshot_id": snap["id"], "groups": [api_group],
                                  "target_bytes": 10 * 1048576})
    check("7 基于真实盘点生成方案成功", code == 200 and pj["plan"] is not None)
    plan_a = pj["plan"]["id"]

    # 5) 注入扫描抖动（偶数序号慢），混合并发 3 复核 + 3 盘点，制造乱序完成
    real_scan = duty.take_snapshot

    def jittered_scan(volume_path, snap_id, seq):
        snap = real_scan(volume_path, snap_id, seq)  # 仍是真实 /proc 扫描
        if seq % 2 == 0:
            time.sleep(0.35)
        return snap

    duty.take_snapshot = jittered_scan
    completion_log = []  # (实际完成时刻, seq)
    log_lock = threading.Lock()

    def mixed(i):
        if i % 2 == 0:
            kind, code, payload = ("review",) + http("/api/review", {"plan_id": plan_a})
        else:
            kind, code, payload = ("inventory",) + http("/api/inventory", {})
        with log_lock:
            completion_log.append((time.time(), payload["snapshot"]["seq"]))
        return kind, code, payload

    with ThreadPoolExecutor(max_workers=6) as ex:
        mixed_results = list(ex.map(mixed, range(6)))
    duty.take_snapshot = real_scan
    check("8 混合并发请求全部成功", all(r[1] == 200 for r in mixed_results),
          str([r[1] for r in mixed_results]))

    # 乱序完成的直接证据：实际完成顺序与序号（开始）顺序不一致
    completion_log.sort(key=lambda x: x[0])
    completion_seqs = [seq for _, seq in completion_log]
    start_order = sorted(completion_seqs)
    code, st = http("/api/state")
    check("9 乱序完成后当前盘点仍为最后开始的那次（最大序号）",
          st["snapshot"]["seq"] == max(completion_seqs),
          f"{st['snapshot']['seq']} vs {completion_seqs}")
    pa = next(p for p in st["plans"] if p["id"] == plan_a)
    review_seqs = [r[2]["review"]["seq"] for r in mixed_results if r[0] == "review"]
    check("10 方案保留最后开始的那次复核",
          pa["review"]["seq"] == max(review_seqs),
          f"{pa['review']['seq']} vs {max(review_seqs)}")
    check("11 确实发生了乱序完成（抖动生效，测试有效）",
          completion_seqs != start_order,
          f"完成顺序 {completion_seqs} vs 开始顺序 {start_order}")

    # 6) 复核真实发现占用变化：对持续增长的 worker.log 持有组做方案再复核
    code, st = http("/api/state")
    snap = st["snapshot"]
    wf = next(f for f in snap["files"] if f["path"].endswith("worker.log"))
    wg = wf["holders"][0]
    code, pj = http("/api/plan", {"snapshot_id": snap["id"], "groups": [wg],
                                  "target_bytes": 1})
    plan_w = pj["plan"]["id"]
    time.sleep(2.5)  # 让 worker.log 再长约 5 MiB
    code, rv = http("/api/review", {"plan_id": plan_w})
    joined = " ".join(rv["review"]["invalidations"])
    check("12 复核真实捕获占用变化并作废方案",
          code == 200 and not rv["review"]["valid"] and "占用变化" in joined, joined)

    # 7) 采集失败：LOG_VOLUME 指向不存在路径（statvfs 抛 OSError），状态不变
    before = http("/api/state")[1]
    duty.LOG_VOLUME = VOL + "/nonexistent-subdir-x"
    code, err = http("/api/inventory", {})
    check("13 采集失败返回 500", code == 500 and "error" in err, str(err))
    code, err2 = http("/api/review", {"plan_id": plan_a})
    check("14 复核采集失败返回 500", code == 500, str(err2))
    duty.LOG_VOLUME = VOL
    after = http("/api/state")[1]
    check("15 失败后当前盘点不变",
          after["snapshot"]["seq"] == before["snapshot"]["seq"])
    pa_before = next(p for p in before["plans"] if p["id"] == plan_a)
    pa_after = next(p for p in after["plans"] if p["id"] == plan_a)
    check("16 失败后方案复核结论不变",
          pa_after["review"]["seq"] == pa_before["review"]["seq"])

    # 8) 失败后新采集仍能成功生效
    code, okj = http("/api/inventory", {})
    check("17 失败后新盘点成功", code == 200)
    code, st = http("/api/state")
    check("18 新盘点已成为当前盘点", st["snapshot"]["seq"] == okj["snapshot"]["seq"])

    print()
    if failures:
        print(f"{len(failures)} 项失败：{failures}")
    else:
        print("全部端到端测试通过。")


if __name__ == "__main__":
    main()
