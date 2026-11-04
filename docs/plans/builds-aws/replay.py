"""Phase 0 replay: natedev's memory queue with and without an AWS box (scratch analysis)."""
import heapq, sqlite3, statistics, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from scripts.buildlog.index import index_path

DB = index_path()
SINCE = "2026-10-24T00:00:00Z"
TICK = 5.0
GIB = 1073741824.0
TEST_KINDS = {"nextest", "check", "doc"}
LINT_KINDS = {"clippy", "mend", "fmt"}
HEAD_S, BOOT_S, IDLE_S, START_WAIT_S, START_MIN = 600.0, 120.0, 900.0, 120.0, 2

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = con.execute(
    "SELECT call_id, caller, step, repo_identity, repo, started_at_us/1e6, coalesce(mem_wait_s,0), duration_s, peak_mem_bytes "
    "FROM steps WHERE host='natedev' AND step<>'sweep' AND started_at>=? AND duration_s IS NOT NULL ORDER BY started_at_us",
    (SINCE,)).fetchall()
peaks = defaultdict(list)
for r in rows:
    if r[8]:
        peaks[(r[4], r[2])].append(r[8] * 0.65 / GIB)
steps = []
for call, caller, kind, ident, repo, start, wait, dur, peak in rows:
    need = peak * 0.65 / GIB if peak else (statistics.median(peaks[(repo, kind)]) if peaks[(repo, kind)] else 2.0)
    steps.append(dict(call=call, caller=caller or "", kind=kind, known=bool(ident) and ident != "unknown",
                      q=start - wait, start=start, wait=wait, dur=max(dur, 1.0), need=max(need, 0.25)))
t0 = min(s["q"] for s in steps)
span_days = (max(s["start"] for s in steps) - t0) / 86400
chains = defaultdict(list)
for i, s in enumerate(steps):
    if s["call"]:
        chains[s["call"]].append(i)
prev = {}
for ids in chains.values():
    ids.sort(key=lambda i: steps[i]["q"])
    for a, b in zip(ids, ids[1:]):
        prev[b] = (a, max(0.0, steps[b]["q"] - (steps[a]["start"] + steps[a]["dur"])))


def eligible(s, lint):
    return s["caller"] == "verify" and (s["kind"] in TEST_KINDS or (lint and s["kind"] in LINT_KINDS))


def sim(budget, box=None, lint=False, f=1.0):
    n = len(steps)
    arrive = [None] * n
    end = [None] * n
    for i, s in enumerate(steps):
        if i not in prev:
            arrive[i] = s["q"]
    pending = [(arrive[i], i) for i in range(n) if arrive[i] is not None]
    heapq.heapify(pending)
    queue, run_n, run_b = [], [], []
    waits = [0.0] * n
    state, ready_at, idle_since, up_s, box_step_s, moved = "off", 0.0, None, 0.0, 0.0, 0
    followers = defaultdict(list)
    for b, (a, gap) in prev.items():
        followers[a].append((b, gap))
    t = t0
    done = 0
    while done < n:
        while pending and pending[0][0] <= t:
            queue.append(heapq.heappop(pending)[1])
        for lst in (run_n, run_b):
            for i in [i for i in lst if end[i] <= t]:
                lst.remove(i); done += 1
                for b, gap in followers[i]:
                    arrive[b] = end[i] + gap
                    heapq.heappush(pending, (arrive[b], b))
        used = sum(steps[i]["need"] for i in run_n)
        reserve = 0.0
        keep = []
        for k, i in enumerate(queue):
            s = steps[i]
            fits = used + s["need"] <= budget - reserve or (not run_n and k == 0)
            if fits:
                run_n.append(i); used += s["need"]; waits[i] = t - arrive[i]; end[i] = t + s["dur"]
            else:
                if k == 0 and t - arrive[i] >= HEAD_S:
                    reserve = s["need"]
                keep.append(i)
        queue = keep
        if box:
            if state == "off":
                if sum(1 for i in queue if eligible(steps[i], lint) and t - arrive[i] >= START_WAIT_S) >= START_MIN:
                    state, ready_at = "boot", t + BOOT_S
            if state == "boot" and t >= ready_at:
                state, idle_since = "up", t
            if state == "up":
                bused = sum(steps[i]["need"] for i in run_b)
                keep = []
                for i in queue:
                    s = steps[i]
                    if eligible(s, lint) and (bused + s["need"] <= box or not run_b):
                        run_b.append(i); bused += s["need"]; waits[i] = t - arrive[i]
                        end[i] = t + s["dur"] * f; box_step_s += s["dur"] * f; moved += 1
                    else:
                        keep.append(i)
                queue = keep
                if run_b:
                    idle_since = None
                elif idle_since is None:
                    idle_since = t
                elif t - idle_since >= IDLE_S:
                    state = "off"
            if state != "off":
                up_s += TICK
        t += TICK
    return sum(waits) / 3600, up_s / 3600, box_step_s / 3600, moved


actual = sum(s["wait"] for s in steps) / 3600
print(f"steps {len(steps)}, span {span_days:.2f} d, actual wait {actual:.0f} h, "
      f"verify+known eligible: tests {sum(eligible(s, False) for s in steps)}, with lint {sum(eligible(s, True) for s in steps)}")
lo, hi = 8.0, 60.0
for _ in range(9):
    mid = (lo + hi) / 2
    w = sim(mid)[0]
    lo, hi = (mid, hi) if w > actual else (lo, mid)
B = (lo + hi) / 2
base = sim(B)[0]
print(f"calibrated natedev budget {B:.1f} GiB -> simulated wait {base:.0f} h")
month = 30 / span_days
SIZES = [("m7a.4xlarge", 0.5, 0.927, 0.483), ("m7a.8xlarge", 1.0, 1.855, 0.893), ("m7a.12xlarge", 1.5, 2.782, 1.34)]
FIXED = 74.0
print("size|lint|f|saved h/mo|natedev wait h/mo|up h/day|box step h/day|steps moved/day|$/mo spot|$/mo OD|$/saved h spot|$/saved h OD")
for name, ratio, od, spot in SIZES:
    for lint in (False, True):
        for f in (1.0, 1.3):
            w, up, bs, mv = sim(B, box=B * ratio, lint=lint, f=f)
            saved = (base - w) * month
            cs, co = up * month * spot + FIXED, up * month * od + FIXED
            print(f"{name}|{'yes' if lint else 'no'}|{f}|{saved:.0f}|{w*month:.0f}|{up/span_days:.1f}|{bs/span_days:.1f}|{mv/span_days:.0f}|"
                  f"{cs:.0f}|{co:.0f}|{cs/saved if saved > 0 else 0:.2f}|{co/saved if saved > 0 else 0:.2f}")
            sys.stdout.flush()
