# xc-vod-dl

Interactive CLI for downloading movies and series from an Xtream Codes (XC) IPTV
server, built for people who already pay for/run their own legal IPTV service and
just want a sane local library — not another paywalled "season downloader" script.

- Browse and search Movies/VOD and Series interactively, or drive it non-interactively
  from a manifest file for scripting.
- **Resume**, not restart: a network hiccup mid-download picks up where it left off.
- **Verification**: every finished file is checked with `ffprobe`/`ffmpeg` before it's
  considered done — a "successful" download that's actually corrupt gets caught.
- **Adaptive parallel downloads**: sizes itself from your account's `max_connections`,
  falls back toward serial automatically if the server starts throttling mid-run.
- **Missing-episode report**: tells you when a season has gaps (e.g. has S01E08 and
  S01E10, flags S01E09 as missing) before you start downloading — including gaps a
  single listing can't see on its own (a missing *trailing* episode), by
  cross-checking duplicate listings of the same show against each other.
- **Optional rename**: rename a movie/series after selecting it (e.g. to strip an
  upstream language-code prefix) — drives the folder/filename/`.nfo` for the whole
  thing, not just one file.
- **Web UI** (optional): `xc-vod-dl serve` for a local browser-based search/select/
  download flow, for anyone who'd rather click than type.

## Requirements

- Python 3.10+
- `ffmpeg`/`ffprobe` on `PATH` (used to verify downloads — auto-detected, or point
  at them explicitly via `config.toml`)
- Your own Xtream Codes server URL + username + password

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"       # CLI + tests
pip install -e ".[web]"       # + the optional web UI (xc-vod-dl serve)
```

## Configure

```bash
cp config.example.toml config.toml   # or ~/.config/xc-vod-dl/config.toml
```

Fill in your server URL, username, and password. `config.toml` is gitignored —
never commit real credentials. Credentials can also be supplied via
`XCVODDL_SERVER` / `XCVODDL_USERNAME` / `XCVODDL_PASSWORD` environment variables,
or `--server` / `--username` / `--password` CLI flags (highest priority first:
CLI flag, env var, config file).

## Usage

### Interactive

```bash
xc-vod-dl browse
```

Walks you through Movies/Series → category → item(s), with a missing-episode
report shown before you commit to downloading a series.

### Scripted (manifest file)

```bash
cat > wanted.txt <<'EOF'
movie:12345
series:6789            # whole series, all seasons
series:6789:2          # season 2 only
series:6789:2:5         # a single episode
EOF

xc-vod-dl fetch -f wanted.txt -y
```

`#` starts a comment; blank lines are ignored. Exit code is `0` if everything
succeeded, `1` on partial/total failure — safe to use in scripts/cron.

### Other commands

```bash
xc-vod-dl gaps --series-id 6789        # report missing episodes, no download
xc-vod-dl resume                       # retry anything left pending/failed in state.db
xc-vod-dl status                       # show what's pending/downloading/failed/done — no side effects
xc-vod-dl clean                        # remove stray .voddl (unverified partial) files
```

`status` groups everything currently tracked in `state.db` by status — exactly
what a `resume` would act on, without actually running one. `done` items are
summarized as a count by default (`--all` to list them individually), `--status
<name>` filters to one group, and `--json` gives a machine-readable dump.

`fetch`/`browse`/`resume` all accept `--serial` (force one-at-a-time),
`--parallel N` (force a specific count), and `--verify-mode quick|full`
to override what's in `config.toml` for a single run.

### Web UI

```bash
xc-vod-dl serve --host 127.0.0.1 --port 8787
```

Opens a local search/select/download page in your browser, backed by the exact
same download engine as `fetch`/`browse` — not a separate implementation. Search
results, gap reports, and the optional rename prompt all work the same way as
the interactive CLI; downloads show live progress and stream to the same
`state.db`, so `resume`/`gaps`/`clean` all still see what it did. Needs the
`web` extra (`pip install xc-vod-dl[web]`); `--server`/`--username`/`--password`/
`--config` work the same as the other commands.

### How resume/verify actually works

Every in-progress file is written as `<name>.<ext>.voddl` — a `.voddl` file
left in a folder is *known-incomplete* and safe to ignore or `xc-vod-dl clean`.
On a network hiccup, the next attempt sends an HTTP `Range` request picking up
from the `.voddl` file's current size rather than starting over. Once a
download completes, `ffprobe` (or `ffmpeg` in `full` mode) checks it's actually
decodable before the file is atomically renamed to its final name — a
"finished" download that's secretly corrupt never gets marked done.

### Downloading to a network share

The media files (`movies_dir`/`series_dir`) are fine on a network share
(CIFS/SMB, NFS, ...). The SQLite `state.db` is not — network filesystems
don't reliably support the file locking SQLite needs for resume tracking,
and running from such a directory tends to fail immediately with
`sqlite3.OperationalError: database is locked`, even with only one process
involved. Point `state_db` in `config.toml` at a local path instead, e.g.:

```toml
[download]
state_db = "/home/you/.local/state/xc-vod-dl/state.db"
```

or pass `--state-db /local/path/state.db` on the command line. The media
itself can still live on the network share.

## Development

```bash
pytest              # fast unit tests + hermetic local-server integration tests
ruff check .
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
