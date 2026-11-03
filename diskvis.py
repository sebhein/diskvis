#!/usr/bin/env python3
"""diskvis: see what is using disk space in WSL, and when it's time to clean up.

  diskvis.py                 scan and print a report
  diskvis.py browse [PATH]   scan, then explore interactively
  diskvis.py check [-q]      instant verdict from the last scan (exit 1 = act)

Read-only: it never deletes anything.
"""

import argparse
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

G = 1024**3

# When to clean up. Going past any of these turns the verdict into "act".
ZIG_LIMIT = 100 * G  # Zig build output is rebuildable, so this much is waste
FREE_LIMIT = 100 * G  # usable free space left for Linux
DEAD_LIMIT = 100 * G  # empty space stuck inside ext4.vhdx
STALE_DAYS = 14  # a Zig cache not written for this long is flagged "stale"

STATE_DIR = Path(
    os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
) / "diskvis"
HISTORY = STATE_DIR / "history.jsonl"
VHDX_CACHE = STATE_DIR / "vhdx-path"

CATEGORIES = {
    "zig_cache": "Zig .zig-cache",
    "zig_out": "Zig zig-out",
    "zig_global": "Zig global cache",
    "worktrees": "Agent worktrees",
    "claude": "Claude sessions",
    "pkg": "Package caches",
    "node_modules": "node_modules",
    "cache": "Other ~/.cache",
    "home": "Other home files",
    "system": "System (outside home)",
}
ZIG_CATEGORIES = ("zig_cache", "zig_out", "zig_global")
ZIG_DIR_NAMES = {".zig-cache": "zig_cache", "zig-cache": "zig_cache", "zig-out": "zig_out"}


# ---------------------------------------------------------------- scanning


class Dir:
    __slots__ = ("name", "parent", "size", "own", "newest", "kids", "category")

    def __init__(self, name, parent=None):
        self.name = name
        self.parent = parent
        self.size = 0  # bytes on disk, including everything below
        self.own = 0  # bytes on disk of files directly in this directory
        self.newest = 0.0  # most recent mtime of any file below
        self.kids = []
        self.category = None

    @property
    def path(self):
        parts = []
        node = self
        while node is not None:
            parts.append(node.name)
            node = node.parent
        return os.path.join(*reversed(parts))


class Scan:
    def __init__(self, root):
        self.root = root
        self.totals = dict.fromkeys(CATEGORIES, 0)
        self.zig_dirs = []  # outermost Zig cache/output directories
        self.unreadable = 0
        self.entries = 0

    @property
    def zig_total(self):
        return sum(self.totals[c] for c in ZIG_CATEGORIES)


def classify(path, name, inherited, home):
    """Which category a directory's contents belong to."""
    if inherited in ZIG_CATEGORIES:
        return inherited
    if name in ZIG_DIR_NAMES:
        return ZIG_DIR_NAMES[name]
    exact = {
        home: "home",
        f"{home}/.cache": "cache",
        f"{home}/.cache/zig": "zig_global",
        f"{home}/.cache/uv": "pkg",
        f"{home}/.npm": "pkg",
        f"{home}/.bun": "pkg",
        f"{home}/go/pkg": "pkg",
        f"{home}/.claude/projects": "claude",
    }.get(path)
    if exact:
        return exact
    if name == "worktrees" and os.path.basename(os.path.dirname(path)) == ".claude":
        return "worktrees"
    if name == "node_modules" and inherited != "worktrees":
        return "node_modules"
    return inherited


def scan(root, home=None, progress=None):
    home = home or str(Path.home())
    root = os.path.realpath(root)
    result = Scan(root)
    device = os.stat(root).st_dev
    seen_links = set()
    inside_home = root == home or root.startswith(home + os.sep)

    def walk(path, node, inherited):
        category = classify(path, node.name, inherited, home)
        node.category = category
        if category in ZIG_CATEGORIES and inherited not in ZIG_CATEGORIES:
            result.zig_dirs.append(node)

        subdirs = []
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    result.entries += 1
                    if progress and result.entries % 50000 == 0:
                        progress(result.entries)
                    if stat.S_ISDIR(st.st_mode):
                        # Staying on one device skips /mnt/c, /proc and friends.
                        if st.st_dev == device:
                            subdirs.append(entry.name)
                        continue
                    if st.st_nlink > 1:
                        key = (st.st_dev, st.st_ino)
                        if key in seen_links:
                            continue
                        seen_links.add(key)
                    node.own += st.st_blocks * 512
                    node.newest = max(node.newest, st.st_mtime)
        except OSError:
            result.unreadable += 1

        result.totals[category] += node.own
        node.size = node.own
        for name in subdirs:
            kid = Dir(name, node)
            walk(os.path.join(path, name), kid, category)
            node.kids.append(kid)
            node.size += kid.size
            node.newest = max(node.newest, kid.newest)
        node.kids.sort(key=lambda d: d.size, reverse=True)

    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))
    result.tree = Dir(root)
    walk(root, result.tree, "home" if inside_home else "system")
    result.zig_dirs.sort(key=lambda d: d.size, reverse=True)
    return result


def scan_with_progress(root):
    tty = sys.stderr.isatty()

    def progress(count):
        if tty:
            print(f"\rScanning {root}: {count:,} entries", end="", file=sys.stderr, flush=True)

    if tty:
        print(f"Scanning {root}...", end="", file=sys.stderr, flush=True)
    result = scan(root, progress=progress)
    if tty:
        print("\r\033[K", end="", file=sys.stderr, flush=True)
    return result


# ------------------------------------------------- capacity: Linux and Windows


def find_vhdx():
    """Path to this distro's ext4.vhdx as seen from Linux, or None."""
    try:
        cached = VHDX_CACHE.read_text().strip()
        if os.path.exists(cached):
            return cached
    except OSError:
        pass
    distro = os.environ.get("WSL_DISTRO_NAME")
    if not distro:
        return None
    command = (
        r"Get-ChildItem HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss | "
        "ForEach-Object { Get-ItemProperty $_.PSPath } | "
        f"Where-Object DistributionName -eq '{distro}' | "
        "Select-Object -ExpandProperty BasePath"
    )
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-c", command],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        base = out.removeprefix("\\\\?\\")
        linux = subprocess.run(
            ["wslpath", "-u", base], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    path = os.path.join(linux, "ext4.vhdx")
    if not linux or not os.path.exists(path):
        return None
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        VHDX_CACHE.write_text(path)
    except OSError:
        pass
    return path


def drive_label(path):
    parts = path.split("/")
    if len(parts) > 2 and parts[1] == "mnt" and len(parts[2]) == 1:
        return parts[2].upper() + ":"
    return "host drive"


def capacity():
    fs = os.statvfs("/")
    total = fs.f_blocks * fs.f_frsize
    used = (fs.f_blocks - fs.f_bfree) * fs.f_frsize
    info = {
        "total": total,
        "used": used,
        "free": fs.f_bavail * fs.f_frsize,
        "usable": fs.f_bavail * fs.f_frsize,
        "vhdx": None,
    }
    vhdx = find_vhdx()
    if vhdx:
        try:
            host = os.statvfs(os.path.dirname(vhdx))
            on_host = os.stat(vhdx).st_blocks * 512
        except OSError:
            return info
        dead = max(0, on_host - used)
        host_free = host.f_bavail * host.f_frsize
        info["vhdx"] = {
            "path": vhdx,
            "drive": drive_label(vhdx),
            "size": on_host,
            "dead": dead,
            "host_total": host.f_blocks * host.f_frsize,
            "host_free": host_free,
        }
        # Linux can reuse the file's dead space, then grow until the drive is full.
        info["usable"] = min(info["free"], dead + host_free)
    return info


def verdict(cap, zig_total):
    """Reasons to act now. Empty means no cleanup is needed yet."""
    reasons = []
    if zig_total is not None and zig_total >= ZIG_LIMIT:
        reasons.append(
            f"Zig build output is {size(zig_total)} (limit {size(ZIG_LIMIT)}), and it can be rebuilt"
        )
    if cap["usable"] < FREE_LIMIT:
        reasons.append(f"Only {size(cap['usable'])} of usable space left for Linux")
    vhdx = cap["vhdx"]
    if vhdx and vhdx["dead"] >= DEAD_LIMIT:
        reasons.append(
            f"ext4.vhdx is holding about {size(vhdx['dead'])} of empty space "
            f"that {vhdx['drive']} can't use until it is compacted"
        )
    return reasons


# ----------------------------------------------------------------- history


def load_history():
    try:
        lines = HISTORY.read_text().splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def append_history(cap, result):
    record = {"t": time.time(), "used": cap["used"], "zig": result.zig_total}
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with HISTORY.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def growth_per_day(records):
    """Growth in bytes/day since the last cleanup, or None if unknown."""
    if len(records) < 2:
        return None
    start = len(records) - 1
    while start > 0 and records[start - 1]["used"] <= records[start]["used"]:
        start -= 1
    days = (records[-1]["t"] - records[start]["t"]) / 86400
    if days < 1:
        return None
    return (records[-1]["used"] - records[start]["used"]) / days


# --------------------------------------------------------------- formatting


def size(n):
    if n >= 100 * G:
        return f"{n / G:.0f}G"
    if n >= G:
        return f"{n / G:.1f}G"
    if n >= 1024**2:
        return f"{n / 1024**2:.0f}M"
    if n >= 1024:
        return f"{n / 1024:.0f}K"
    return f"{n}B"


def age(timestamp):
    if not timestamp:
        return "empty"
    seconds = max(0, time.time() - timestamp)
    if seconds < 3600:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 86400:
        return f"{seconds / 3600:.0f}h ago"
    return f"{seconds / 86400:.0f}d ago"


def bar(fraction, width=20):
    fraction = min(max(fraction, 0.0), 1.0)
    filled = round(fraction * width)
    if fraction > 0 and filled == 0:
        filled = 1
    return "█" * filled + "░" * (width - filled)


def shorten(path, home=None):
    home = home or str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


class Style:
    def __init__(self, enabled):
        self.enabled = enabled

    def __call__(self, code, text):
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text):
        return self("1", text)

    def dim(self, text):
        return self("2", text)

    def red(self, text):
        return self("31", text)

    def green(self, text):
        return self("32", text)

    def yellow(self, text):
        return self("33", text)


def print_tree(node, total, style, prefix="", depth=0):
    show_at = max(total * 0.01, 1)
    expand_at = total * 0.04
    kids = [k for k in node.kids if k.size >= show_at]
    hidden = node.size - sum(k.size for k in kids)
    for i, kid in enumerate(kids):
        last = i == len(kids) - 1 and hidden < show_at
        # Fold single-child chains such as home/seb into one row.
        label = kid.name
        current = kid
        while (
            current.category not in ZIG_CATEGORIES
            and current.kids
            and current.kids[0].size >= current.size * 0.95
        ):
            current = current.kids[0]
            label += "/" + current.name
        branch = "└─ " if last else "├─ "
        name = label
        if current.category in ZIG_CATEGORIES:
            name = style.yellow(label)
        print(
            f"  {size(kid.size):>6} {style.dim(bar(kid.size / total, 16))} "
            f"{prefix}{branch}{name}"
        )
        if current.size >= expand_at and depth < 5 and current.category not in ZIG_CATEGORIES:
            print_tree(current, total, style, prefix + ("   " if last else "│  "), depth + 1)
    if hidden >= show_at:
        print(f"  {size(hidden):>6} {' ' * 16} {prefix}└─ {style.dim('(smaller items)')}")


def print_cleanup_steps(vhdx, style):
    print(style.bold("How to clean up"))
    print("  1. Delete Zig caches. The next build of each project will be slow:")
    print("       find ~ -xdev -type d -name .zig-cache -prune -exec rm -rf {} +")
    print("       rm -rf ~/.cache/zig")
    if vhdx:
        windows = vhdx["path"].replace(f"/mnt/{vhdx['drive'][0].lower()}/", vhdx["drive"] + "\\")
        windows = windows.replace("/", "\\")
        print(f"  2. Hand the space back to {vhdx['drive']}. Deleting alone doesn't shrink ext4.vhdx:")
        print("       sudo fstrim -a")
        print("     Then from an administrator prompt on Windows (this stops all of WSL):")
        print("       wsl --shutdown")
        print("       diskpart")
        print(f'         select vdisk file="{windows}"')
        print("         compact vdisk")
        print("         exit")
    print()


def report(result, cap, records, style):
    now = time.strftime("%Y-%m-%d %H:%M")
    print(f"\n{style.bold('diskvis')}  {result.root}  {style.dim(now)}\n")

    is_full_scan = result.root == "/"
    reasons = verdict(cap, result.zig_total if is_full_scan else None)
    if reasons:
        print(style.red(style.bold("TIME TO CLEAN UP")))
        for reason in reasons:
            print(f"  - {reason}")
    else:
        print(style.green(style.bold("OK")) + "  no cleanup needed yet")
    print()

    print(style.bold("Capacity"))
    print(
        f"  Linux disk   {bar(cap['used'] / cap['total'])} {size(cap['used'])} used of "
        f"{size(cap['total'])}, {size(cap['usable'])} actually usable"
    )
    vhdx = cap["vhdx"]
    if vhdx:
        used = vhdx["host_total"] - vhdx["host_free"]
        print(
            f"  Drive {vhdx['drive']:<6} {bar(used / vhdx['host_total'])} {size(used)} used of "
            f"{size(vhdx['host_total'])}, {size(vhdx['host_free'])} free"
        )
        print(
            f"  ext4.vhdx    {bar(vhdx['size'] / vhdx['host_total'])} {size(vhdx['size'])} on "
            f"{vhdx['drive']}, of which about {size(vhdx['dead'])} is empty"
        )
    rate = growth_per_day(records)
    if rate is not None and rate > 0:
        print(
            f"  Growth       {size(int(rate))}/day since the last cleanup, "
            f"full in about {cap['usable'] / rate:.0f} days"
        )
    print()

    print(style.bold("By kind"))
    biggest = max(result.totals.values()) or 1
    for key, total in sorted(result.totals.items(), key=lambda kv: kv[1], reverse=True):
        if total < G / 10:
            continue
        label = f"{CATEGORIES[key]:<22}"
        if key in ZIG_CATEGORIES:
            label = style.yellow(label)
        print(f"  {label} {size(total):>6} {style.dim(bar(total / biggest))}")
    print()

    print(style.bold("Where"))
    print_tree(result.tree, max(result.tree.size, 1), style)
    print()

    if result.zig_dirs:
        print(style.bold(f"Largest Zig directories ({len(result.zig_dirs)} total)"))
        for node in result.zig_dirs[:12]:
            stale = time.time() - node.newest > STALE_DAYS * 86400
            when = f"written {age(node.newest)}"
            when = style.yellow(f"{when}, stale") if stale else style.dim(when)
            print(f"  {size(node.size):>6}  {shorten(node.path)}  {when}")
        rest = result.zig_dirs[12:]
        if rest:
            print(f"  {size(sum(d.size for d in rest)):>6}  {style.dim(f'{len(rest)} more')}")
        print()

    if reasons:
        print_cleanup_steps(vhdx, style)

    missing = cap["used"] - result.tree.size
    if is_full_scan and missing > G:
        print(style.dim(f"{size(missing)} of used space wasn't found by the scan (filesystem overhead or root-only files)."))
    if result.unreadable:
        print(style.dim(f"{result.unreadable} directories couldn't be read. Run with sudo to include them."))


# ------------------------------------------------------------------ browser


def browse(result):
    import curses

    def ui(screen):
        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_YELLOW, -1)
        node = result.tree
        cursor = {}  # remembers the selected row for each directory

        while True:
            rows = list(node.kids)
            height, width = screen.getmaxyx()
            screen.erase()

            def put(y, text, attr=0):
                if 0 <= y < height:
                    try:
                        screen.addnstr(y, 0, text, max(width - 1, 0), attr)
                    except curses.error:
                        pass

            selected = min(cursor.get(id(node), 0), max(len(rows) - 1, 0))
            view = max(height - 4, 1)
            top = max(0, selected - view + 1)
            put(0, f"{shorten(node.path)}  {size(node.size)}", curses.A_BOLD)
            put(1, f"{size(node.own)} in files directly here" if node.own else "")
            for i, kid in enumerate(rows[top : top + view]):
                attr = curses.A_REVERSE if top + i == selected else 0
                if kid.category in ZIG_CATEGORIES:
                    attr |= curses.color_pair(1)
                share = kid.size / node.size if node.size else 0
                put(
                    2 + i,
                    f"{size(kid.size):>7} {bar(share, 16)} {share * 100:3.0f}%  "
                    f"{kid.name}/  {age(kid.newest)}",
                    attr,
                )
            if not rows:
                put(2, "(no subdirectories)", curses.A_DIM)
            put(height - 1, "up/down move  enter open  backspace back  q quit", curses.A_DIM)
            screen.refresh()

            key = screen.getch()
            if key in (ord("q"), 27):
                return
            if key in (curses.KEY_UP, ord("k")):
                selected = max(selected - 1, 0)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = min(selected + 1, max(len(rows) - 1, 0))
            elif key == curses.KEY_PPAGE:
                selected = max(selected - view, 0)
            elif key == curses.KEY_NPAGE:
                selected = min(selected + view, max(len(rows) - 1, 0))
            elif key in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT, ord("l")):
                cursor[id(node)] = selected
                if rows:
                    node = rows[selected]
                continue
            elif key in (curses.KEY_BACKSPACE, 127, 8, curses.KEY_LEFT, ord("h")):
                cursor[id(node)] = selected
                if node.parent is not None:
                    node = node.parent
                continue
            cursor[id(node)] = selected

    curses.wrapper(ui)


# --------------------------------------------------------------------- main


def run_check(quiet):
    records = load_history()
    cap = capacity()
    zig = records[-1]["zig"] if records else None
    reasons = verdict(cap, zig)
    if reasons:
        print("diskvis: time to clean up. " + "; ".join(reasons) + ".")
        return 1
    if not quiet:
        scanned = f"last scan {age(records[-1]['t'])}" if records else "not scanned yet"
        print(f"diskvis: OK, {size(cap['usable'])} usable ({scanned}).")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="See what is using disk space in WSL, and when to clean up. Never deletes anything."
    )
    parser.add_argument("command", nargs="?", default="report", choices=["report", "browse", "check"])
    parser.add_argument("path", nargs="?", default="/", help="where to scan (default: /)")
    parser.add_argument("-q", "--quiet", action="store_true", help="check: print nothing when OK")
    args = parser.parse_args()
    sys.stdout.reconfigure(errors="replace")

    if args.command == "check":
        return run_check(args.quiet)

    if not os.path.isdir(args.path):
        parser.error(f"not a directory: {args.path}")
    result = scan_with_progress(args.path)

    if args.command == "browse":
        browse(result)
        return 0

    cap = capacity()
    if result.root == "/":
        append_history(cap, result)
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    report(result, cap, load_history(), Style(color))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)
