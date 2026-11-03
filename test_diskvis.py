import os
import tempfile
import time
import unittest

import diskvis
from diskvis import G


def write(path, kb=64):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(os.urandom(kb * 1024))
        f.flush()
        os.fsync(f.fileno())


def cap(usable=500 * G, dead=0, used=100 * G):
    return {
        "total": 1000 * G,
        "used": used,
        "free": usable,
        "usable": usable,
        "vhdx": {
            "path": "/mnt/s/wsl/ext4.vhdx",
            "drive": "S:",
            "size": used + dead,
            "dead": dead,
            "host_total": 1000 * G,
            "host_free": 500 * G,
        },
    }


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self):
        return diskvis.scan(self.home, home=self.home)

    def test_sizes_roll_up_to_parents(self):
        write(f"{self.home}/a/b/file")
        write(f"{self.home}/a/file")
        result = self.scan()
        a = result.tree.kids[0]
        self.assertEqual(a.name, "a")
        self.assertEqual(a.size, a.own + a.kids[0].size)
        self.assertEqual(result.tree.size, a.size)
        self.assertGreater(a.kids[0].size, 0)

    def test_categories_count_each_file_once(self):
        write(f"{self.home}/repo/.zig-cache/o/x")
        write(f"{self.home}/repo/zig-out/bin/y")
        write(f"{self.home}/.cache/zig/z")
        write(f"{self.home}/.cache/other/w")
        write(f"{self.home}/repo/.claude/worktrees/a/src")
        write(f"{self.home}/repo/.claude/worktrees/a/.zig-cache/q")
        write(f"{self.home}/repo/src")
        result = self.scan()
        by_cat = {k: v for k, v in result.totals.items() if v}
        self.assertEqual(
            set(by_cat), {"zig_cache", "zig_out", "zig_global", "cache", "worktrees", "home"}
        )
        self.assertEqual(sum(by_cat.values()), result.tree.size)
        # .zig-cache inside a worktree counts as Zig, not as the worktree.
        self.assertEqual(result.totals["zig_cache"], 2 * result.totals["zig_out"])

    def test_home_files_are_not_system_when_scanning_from_above_home(self):
        home = f"{self.home}/home/me"
        write(f"{home}/notes")
        write(f"{self.home}/usr/lib/x")
        totals = diskvis.scan(self.home, home=home).totals
        self.assertGreater(totals["home"], 0)
        self.assertEqual(totals["home"], totals["system"])

    def test_zig_dirs_lists_only_outermost(self):
        write(f"{self.home}/r/.zig-cache/zig-cache/deep/x")
        result = self.scan()
        self.assertEqual([d.path for d in result.zig_dirs], [f"{self.home}/r/.zig-cache"])

    def test_hard_links_counted_once(self):
        write(f"{self.home}/a/file")
        one = self.scan().tree.size
        os.makedirs(f"{self.home}/b")
        os.link(f"{self.home}/a/file", f"{self.home}/b/link")
        self.assertEqual(self.scan().tree.size, one)

    def test_symlinks_not_followed(self):
        write(f"{self.home}/a/file")
        os.symlink(f"{self.home}/a", f"{self.home}/loop")
        result = self.scan()
        self.assertEqual([k.name for k in result.tree.kids], ["a"])

    def test_unreadable_directory_is_reported(self):
        if os.geteuid() == 0:
            self.skipTest("root can read everything")
        os.makedirs(f"{self.home}/locked")
        os.chmod(f"{self.home}/locked", 0)
        try:
            self.assertEqual(self.scan().unreadable, 1)
        finally:
            os.chmod(f"{self.home}/locked", 0o755)


class VerdictTest(unittest.TestCase):
    def test_ok_when_under_all_limits(self):
        self.assertEqual(diskvis.verdict(cap(), 10 * G), [])

    def test_each_limit_triggers(self):
        self.assertIn("Zig", diskvis.verdict(cap(), 100 * G)[0])
        self.assertIn("usable", diskvis.verdict(cap(usable=99 * G), 0)[0])
        self.assertIn("ext4.vhdx", diskvis.verdict(cap(dead=100 * G), 0)[0])

    def test_unknown_zig_total_is_tolerated(self):
        self.assertEqual(diskvis.verdict(cap(), None), [])


class GrowthTest(unittest.TestCase):
    def test_needs_a_day_of_data(self):
        now = time.time()
        records = [{"t": now - 3600, "used": 0}, {"t": now, "used": G}]
        self.assertIsNone(diskvis.growth_per_day(records))

    def test_measures_from_the_last_cleanup(self):
        day = 86400
        records = [
            {"t": 0, "used": 900 * G},
            {"t": day, "used": 100 * G},  # cleaned up here
            {"t": 3 * day, "used": 120 * G},
        ]
        self.assertAlmostEqual(diskvis.growth_per_day(records), 10 * G)


if __name__ == "__main__":
    unittest.main()
