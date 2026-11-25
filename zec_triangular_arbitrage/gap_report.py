"""Summarize the bot's log: how close each route came to a profitable cycle.

    journalctl -u zecarb -o cat | .venv/bin/python gap_report.py      # running as a service
    journalctl -u zecarb -o cat --since "24 hours ago" | .venv/bin/python gap_report.py
    .venv/bin/python gap_report.py paper.log                          # a saved log file
"""
import re, sys
from statistics import median

WINDOW = re.compile(r"^(\S+ \S+) INFO Best in window: (.*)")
ROUTE = re.compile(r"([AB]): \S+ return=(-?[\d.]+)%")
LEVELS = (0.3, 0.0, -0.1, -0.2)  # % net return; 0.3 is the default signal threshold


def report(lines):
    rows, windows, stale, signals, first, last = [], 0, 0, 0, None, None
    for line in lines:
        if " SIGNAL " in line:
            signals += 1
        elif "No fresh" in line:
            stale += 1
        m = WINDOW.match(line)
        if m:
            windows += 1
            first, last = first or m.group(1), m.group(1)
            rows += [(float(r), m.group(1), route) for route, r in ROUTE.findall(m.group(2))]
    if not windows:
        return "No 'Best in window' lines found. Is the bot running, and is this its log?"
    out = [f"{windows} windows (10s each) from {first} to {last}; "
           f"{stale} with stale/thin books; {signals} SIGNAL lines"]
    for route in "AB":
        xs = [r for r, _, rt in rows if rt == route]
        if xs:
            counts = "  ".join(f">={lv:+.1f}%: {sum(x >= lv for x in xs)}" for lv in LEVELS)
            out.append(f"route {route}: best {max(xs):+.4f}%  median {median(xs):+.4f}%  windows {counts}")
    out.append("10 best windows:")
    out += [f"  {t}  route {route}  {r:+.4f}%" for r, t, route in sorted(rows, reverse=True)[:10]]
    return "\n".join(out)


if __name__ == "__main__":
    print(report(open(sys.argv[1]) if len(sys.argv) > 1 else sys.stdin))
