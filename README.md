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

- Zig build output reaches 100G
- Linux has less than 100G usable
- The virtual disk holds 100G or more of empty space

The limits are constants at the top of `diskvis.py`.

Each full report of `/` also records usage in `~/.local/state/diskvis/`. Once
those reports span at least a day, it shows growth per day and roughly when the
disk fills.

## Tests

```bash
python3 test_diskvis.py
```

## License

[MIT](LICENSE)
