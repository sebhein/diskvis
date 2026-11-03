<p>
  <img src="assets/stamp.svg" alt="Warning: vibe slopped" width="240">
</p>

<p align="center">
  <img src="assets/logo.svg" alt="" width="72">
</p>

<h1 align="center">diskvis</h1>

See what is using disk space in WSL, and when it's time to clean up. Built with
Zig caches in mind, since they grow without limit. It never deletes anything.

Needs only Python 3.

```bash
./diskvis.py                 # scan and print a report
./diskvis.py browse ~/repos  # explore folders interactively
./diskvis.py check           # instant verdict from the last report's scan (exit 1 = act)
./diskvis.py check -q        # same, but silent when there's nothing to do
```

## When it says to clean up

The report says **TIME TO CLEAN UP** when any of these is true:

- Zig build output reaches 500G
- Linux has less than 100G usable

The limits are constants at the top of `diskvis.py`.

Each full report of `/` also records usage in `~/.local/state/diskvis/`. Once
those reports span at least a day, it shows growth per day and roughly when the
disk fills.

## Getting warned automatically

A daily systemd timer keeps the numbers current. It assumes the repo is at
`~/repos/diskvis`, and that systemd is enabled in WSL:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/diskvis.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now diskvis.timer
```

Then add this to `~/.bashrc` to see a warning in each new terminal when a limit
is passed. It prints nothing otherwise:

```bash
"$HOME/repos/diskvis/diskvis.py" check -q || true
```

## Tests

```bash
python3 test_diskvis.py
```

## License

[MIT](LICENSE)
