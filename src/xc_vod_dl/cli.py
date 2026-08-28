from __future__ import annotations

import dataclasses
import json
import logging
import re
import sys
from collections.abc import Callable
from pathlib import Path

import click
import requests

from xc_vod_dl import __version__
from xc_vod_dl.api.client import XtreamClient
from xc_vod_dl.api.models import Episode, SeriesInfo, VodInfo
from xc_vod_dl.config import Config, load_config
from xc_vod_dl.download.concurrency import ConcurrencyController, initial_parallelism
from xc_vod_dl.download.engine import DownloadEngine, DownloadJob, run_many
from xc_vod_dl.exceptions import ConfigError, XcVodDlError
from xc_vod_dl.gaps import (
    detect_duplicate_episodes_in_series,
    detect_gaps_in_series,
    format_gap_report,
)
from xc_vod_dl.jobs import JobSpec, parse_manifest
from xc_vod_dl.nfo import build_episode_nfo, build_movie_nfo, build_series_nfo
from xc_vod_dl.state.store import StateStore
from xc_vod_dl.ui.interactive import browse_and_select
from xc_vod_dl.ui.progress import ProgressReporter

logger = logging.getLogger("xc_vod_dl")


@click.group()
@click.version_option(version=__version__, prog_name="xc-vod-dl")
def main() -> None:
    """Interactive downloader for Xtream Codes movies and series."""


def _load_config_or_exit(
    config_path: Path | None, server: str | None, username: str | None, password: str | None
) -> Config:
    try:
        return load_config(config_path, server=server, username=username, password=password)
    except ConfigError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)


def _setup_logging(config: Config, quiet: bool) -> None:
    """Console respects --quiet; the configured logfile (if any) always gets
    everything at INFO+ with a timestamp regardless of --quiet — including
    the concurrency controller's step-up/step-down decisions, which
    otherwise only ever existed in a terminal you'd already closed by the
    time you wanted to know whether a run had actually hammered the server."""
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    console_handler.setLevel(logging.WARNING if quiet else logging.INFO)
    handlers: list[logging.Handler] = [console_handler]

    if config.download.logfile:
        file_handler = logging.FileHandler(config.download.logfile)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        file_handler.setLevel(logging.INFO)
        handlers.append(file_handler)

    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)


def _sanitize(name: str) -> str:
    """Strip characters that are unsafe as path components on common filesystems."""
    cleaned = "".join(c for c in name if c not in '/\\:*?"<>|').strip()
    return cleaned or "untitled"


def _movie_target(config: Config, vod: VodInfo) -> Path:
    year = vod.release_date[:4] if vod.release_date[:4].isdigit() else None
    already_has_year = bool(re.search(r"\(\d{4}\)\s*$", vod.name))
    label = _sanitize(f"{vod.name} ({year})" if year and not already_has_year else vod.name)
    return config.download.movies_dir / label / f"{label}.{vod.container_extension}"


def _episode_target(config: Config, series_name: str, episode: Episode) -> Path:
    series_label = _sanitize(series_name)
    ep_tag = f"S{episode.season:02d}E{episode.episode_num:02d}"
    filename = _sanitize(
        f"{series_label} - {ep_tag} - {episode.title}" if episode.title else f"{series_label} - {ep_tag}"
    )
    return (
        config.download.series_dir
        / series_label
        / f"Season {episode.season:02d}"
        / f"{filename}.{episode.container_extension}"
    )


def _download_cover(session: requests.Session, url: str, target_path: Path) -> None:
    if target_path.exists():
        return
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("could not fetch cover art from %s: %s", url, exc)
        return
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(resp.content)


def _resolve_movie(
    client: XtreamClient,
    config: Config,
    session: requests.Session,
    vod_id: int,
    display_name: str | None = None,
) -> tuple[DownloadJob, Callable[[], None]]:
    vod = client.get_vod_info(vod_id)
    if display_name:
        vod = dataclasses.replace(vod, name=display_name)
    target = _movie_target(config, vod)
    url = client.movie_url(vod.stream_id, vod.container_extension)
    job = DownloadJob(
        id=f"movie:{vod_id}",
        url=url,
        target_path=target,
        kind="movie",
        title=vod.name,
        container_extension=vod.container_extension,
    )

    def write_metadata() -> None:
        if config.download.download_nfo or config.download.download_cover:
            target.parent.mkdir(parents=True, exist_ok=True)
        if config.download.download_nfo:
            target.with_suffix(".nfo").write_text(build_movie_nfo(vod), encoding="utf-8")
        if config.download.download_cover and vod.cover:
            _download_cover(session, vod.cover, target.parent / "cover.png")

    return job, write_metadata


def _resolve_series(
    client: XtreamClient,
    config: Config,
    session: requests.Session,
    series_id: int,
    season: int | None,
    episode: int | None,
    display_name: str | None = None,
) -> tuple[list[DownloadJob], list[Callable[[], None]]]:
    series = client.get_series_info(series_id)
    if display_name:
        series = dataclasses.replace(series, name=display_name)
    gap_map = detect_gaps_in_series(series)
    dupe_map = detect_duplicate_episodes_in_series(series)
    if gap_map or dupe_map:
        click.echo(format_gap_report(series.name, gap_map, dupe_map), err=True)

    series_dir = config.download.series_dir / _sanitize(series.name)

    def write_series_metadata() -> None:
        if config.download.download_nfo or config.download.download_cover:
            series_dir.mkdir(parents=True, exist_ok=True)
        if config.download.download_nfo:
            (series_dir / "tvshow.nfo").write_text(build_series_nfo(series), encoding="utf-8")
        if config.download.download_cover and series.cover:
            _download_cover(session, series.cover, series_dir / "cover.png")

    jobs: list[DownloadJob] = []
    writers: list[Callable[[], None]] = [write_series_metadata]
    seen_targets: set[Path] = set()

    for season_num in sorted(series.episodes):
        if season is not None and season_num != season:
            continue
        for ep in sorted(series.episodes[season_num], key=lambda e: e.episode_num):
            if episode is not None and ep.episode_num != episode:
                continue
            target = _episode_target(config, series.name, ep)
            if target in seen_targets:
                # Two distinct episodes landed on the same season+episode_num+title
                # (seen in the wild: sloppy upstream metadata mislabeling one
                # episode's title as another's). Disambiguate by episode_id
                # rather than let two parallel downloads race to write the
                # same .voddl file.
                target = target.with_stem(f"{target.stem} [{ep.episode_id}]")
            seen_targets.add(target)
            url = client.episode_url(ep.episode_id, ep.container_extension)
            ep_tag = f"S{ep.season:02d}E{ep.episode_num:02d}"
            jobs.append(
                DownloadJob(
                    id=f"episode:{ep.episode_id}",
                    url=url,
                    target_path=target,
                    kind="episode",
                    title=f"{ep_tag} - {ep.title}" if ep.title else f"{series.name} {ep_tag}",
                    series_id=str(series_id),
                    season=ep.season,
                    episode_num=ep.episode_num,
                    container_extension=ep.container_extension,
                )
            )
            writers.append(_make_episode_writer(config, series.name, ep, target))

    return jobs, writers


def _make_episode_writer(
    config: Config, series_name: str, episode: Episode, target: Path
) -> Callable[[], None]:
    def write() -> None:
        if config.download.download_nfo:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.with_suffix(".nfo").write_text(
                build_episode_nfo(episode, series_name), encoding="utf-8"
            )

    return write


def _resolve_jobs(
    client: XtreamClient, config: Config, session: requests.Session, specs: list[JobSpec]
) -> tuple[list[DownloadJob], list[Callable[[], None]]]:
    jobs: list[DownloadJob] = []
    writers: list[Callable[[], None]] = []
    for spec in specs:
        try:
            if spec.kind == "movie":
                job, writer = _resolve_movie(
                    client, config, session, spec.id, spec.display_name
                )
                jobs.append(job)
                writers.append(writer)
            else:
                new_jobs, new_writers = _resolve_series(
                    client, config, session, spec.id, spec.season, spec.episode, spec.display_name
                )
                jobs.extend(new_jobs)
                writers.extend(new_writers)
        except XcVodDlError as exc:
            click.echo(f"warning: skipping {spec.kind}:{spec.id}: {exc}", err=True)
    return jobs, writers


def _run_pipeline(
    client: XtreamClient,
    config: Config,
    session: requests.Session,
    jobs: list[DownloadJob],
    state: StateStore,
    *,
    serial: bool,
    parallel_override: int | None,
    verify_mode: str,
    quiet: bool,
) -> dict[str, bool]:
    """Shared by `fetch`/`browse`/`resume`: given resolved jobs and an open
    StateStore, run them through either the serial engine or the
    concurrency-controlled parallel engine, with a live progress display
    unless --quiet.

    Every job is registered as "pending" up front, before any download
    starts — not lazily as each one happens to reach the front of the
    parallel pool. Without this, killing a run early (Ctrl+C) leaves
    state.db only knowing about whichever handful of items a worker thread
    had actually started on; everything still queued behind the
    concurrency ceiling was never written down, so `resume` would silently
    skip it and a "complete" series would quietly be missing episodes.
    upsert_pending() is idempotent, so re-registering already-known jobs
    (e.g. when this is called from `resume`) is a harmless no-op."""
    for job in jobs:
        state.upsert_pending(
            id=job.id,
            kind=job.kind,
            title=job.title,
            target_path=str(job.target_path),
            series_id=job.series_id,
            season=job.season,
            episode_num=job.episode_num,
            container_extension=job.container_extension,
        )

    try:
        account = client.get_account()
    except XcVodDlError as exc:
        click.echo(f"error: could not reach server: {exc}", err=True)
        sys.exit(2)

    reporter = None if quiet else ProgressReporter()
    if reporter is not None:
        with reporter:
            return _run_jobs(
                client, config, account, session, jobs, state, serial, parallel_override, verify_mode, reporter
            )
    return _run_jobs(
        client, config, account, session, jobs, state, serial, parallel_override, verify_mode, None
    )


def _run_jobs(
    client: XtreamClient,
    config: Config,
    account,
    session: requests.Session,
    jobs: list[DownloadJob],
    state: StateStore,
    serial: bool,
    parallel_override: int | None,
    verify_mode: str,
    reporter: ProgressReporter | None,
) -> dict[str, bool]:
    if serial or parallel_override == 1:
        engine = DownloadEngine(session, state, verify_mode=verify_mode)
        results = {}
        for job in jobs:
            cb = (lambda n, jid=job.id: reporter.report(jid, n)) if reporter else None
            total_cb = (lambda n, jid=job.id: reporter.set_total(jid, n)) if reporter else None
            start_cb = (lambda jid=job.id, title=job.title: reporter.start(jid, title)) if reporter else None
            complete_cb = (lambda ok, jid=job.id: reporter.complete(jid, ok)) if reporter else None
            results[job.id] = engine.run(
                job, progress_cb=cb, total_cb=total_cb, start_cb=start_cb, complete_cb=complete_cb
            )
        return results

    initial = parallel_override or initial_parallelism(
        account.max_connections,
        account.active_cons,
        safety_margin=config.concurrency.safety_margin,
        ceiling=config.concurrency.max_parallel_ceiling,
    )
    controller = ConcurrencyController(
        initial=initial,
        maximum=config.concurrency.max_parallel_ceiling,
        cooldown_s=config.concurrency.cooldown_s,
        recovery_streak=config.concurrency.recovery_streak,
        collapse_floor_pct=config.concurrency.collapse_floor_pct,
    )
    return run_many(
        jobs,
        session_factory=requests.Session,
        state=state,
        controller=controller,
        verify_mode=verify_mode,
        progress_cb=reporter.report if reporter else None,
        total_cb=reporter.set_total if reporter else None,
        start_cb=reporter.start if reporter else None,
        complete_cb=reporter.complete if reporter else None,
    )


@main.command()
@click.option(
    "-f",
    "--file",
    "manifest_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Manifest of movie:/series: lines to fetch (see README for the format).",
)
@click.option("--server", help="Overrides the configured server URL.")
@click.option("--username", help="Overrides the configured username.")
@click.option("--password", help="Overrides the configured password.")
@click.option(
    "--config", "config_path", type=click.Path(path_type=Path), help="Path to config.toml."
)
@click.option("--serial", is_flag=True, help="Force one-at-a-time downloads.")
@click.option(
    "--parallel", "parallel_override", type=int, help="Force a specific parallel download count."
)
@click.option("--verify-mode", type=click.Choice(["quick", "full"]), help="Override verify mode.")
@click.option(
    "--state-db", "state_db_override", type=click.Path(path_type=Path), help="Override state.db path."
)
@click.option("-y", "--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--quiet", is_flag=True, help="Only log warnings/errors.")
def fetch(
    manifest_file: Path,
    server: str | None,
    username: str | None,
    password: str | None,
    config_path: Path | None,
    serial: bool,
    parallel_override: int | None,
    verify_mode: str | None,
    state_db_override: Path | None,
    yes: bool,
    quiet: bool,
) -> None:
    """Download everything listed in a manifest file (non-interactive)."""
    config = _load_config_or_exit(config_path, server, username, password)
    _setup_logging(config, quiet)
    resolved_verify_mode = verify_mode or config.download.verify_mode
    state_db_path = state_db_override or config.download.state_db

    try:
        specs = parse_manifest(manifest_file)
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)

    if not specs:
        click.echo("manifest is empty, nothing to do")
        return

    client = XtreamClient(config.account.server, config.account.username, config.account.password)
    try:
        client.get_account()
    except XcVodDlError as exc:
        click.echo(f"error: could not reach server: {exc}", err=True)
        sys.exit(2)

    session = requests.Session()
    jobs, metadata_writers = _resolve_jobs(client, config, session, specs)

    if not jobs:
        click.echo("nothing resolved to download", err=True)
        sys.exit(1)

    click.echo(f"{len(jobs)} item(s) queued")
    logger.info("%d item(s) queued", len(jobs))
    if not yes and not click.confirm("Proceed with download?", default=True):
        return

    for writer in metadata_writers:
        writer()

    with StateStore(state_db_path) as state:
        results = _run_pipeline(
            client,
            config,
            session,
            jobs,
            state,
            serial=serial,
            parallel_override=parallel_override,
            verify_mode=resolved_verify_mode,
            quiet=quiet,
        )

    succeeded = sum(1 for ok in results.values() if ok)
    failed = len(results) - succeeded
    click.echo(f"done: {succeeded} succeeded, {failed} failed")
    logger.info("done: %d succeeded, %d failed", succeeded, failed)
    sys.exit(0 if failed == 0 else 1)


@main.command()
@click.option("--server", help="Overrides the configured server URL.")
@click.option("--username", help="Overrides the configured username.")
@click.option("--password", help="Overrides the configured password.")
@click.option(
    "--config", "config_path", type=click.Path(path_type=Path), help="Path to config.toml."
)
@click.option("--serial", is_flag=True, help="Force one-at-a-time downloads.")
@click.option(
    "--parallel", "parallel_override", type=int, help="Force a specific parallel download count."
)
@click.option("--verify-mode", type=click.Choice(["quick", "full"]), help="Override verify mode.")
@click.option(
    "--state-db", "state_db_override", type=click.Path(path_type=Path), help="Override state.db path."
)
def browse(
    server: str | None,
    username: str | None,
    password: str | None,
    config_path: Path | None,
    serial: bool,
    parallel_override: int | None,
    verify_mode: str | None,
    state_db_override: Path | None,
) -> None:
    """Interactively browse and select movies/series to download."""
    config = _load_config_or_exit(config_path, server, username, password)
    _setup_logging(config, quiet=False)
    resolved_verify_mode = verify_mode or config.download.verify_mode
    state_db_path = state_db_override or config.download.state_db

    client = XtreamClient(config.account.server, config.account.username, config.account.password)
    try:
        client.get_account()
    except XcVodDlError as exc:
        click.echo(f"error: could not reach server: {exc}", err=True)
        sys.exit(2)

    specs = browse_and_select(client)
    if not specs:
        click.echo("nothing selected")
        return

    session = requests.Session()
    jobs, metadata_writers = _resolve_jobs(client, config, session, specs)
    if not jobs:
        click.echo("nothing resolved to download", err=True)
        sys.exit(1)

    click.echo(f"{len(jobs)} item(s) queued")
    logger.info("%d item(s) queued", len(jobs))
    if not click.confirm("Proceed with download?", default=True):
        return

    for writer in metadata_writers:
        writer()

    with StateStore(state_db_path) as state:
        results = _run_pipeline(
            client,
            config,
            session,
            jobs,
            state,
            serial=serial,
            parallel_override=parallel_override,
            verify_mode=resolved_verify_mode,
            quiet=False,
        )

    succeeded = sum(1 for ok in results.values() if ok)
    failed = len(results) - succeeded
    click.echo(f"done: {succeeded} succeeded, {failed} failed")
    logger.info("done: %d succeeded, %d failed", succeeded, failed)
    sys.exit(0 if failed == 0 else 1)


@main.command()
@click.option("--series-id", required=True, type=int)
@click.option("--server", help="Overrides the configured server URL.")
@click.option("--username", help="Overrides the configured username.")
@click.option("--password", help="Overrides the configured password.")
@click.option(
    "--config", "config_path", type=click.Path(path_type=Path), help="Path to config.toml."
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def gaps(
    series_id: int,
    server: str | None,
    username: str | None,
    password: str | None,
    config_path: Path | None,
    as_json: bool,
) -> None:
    """Report missing episodes in a series without downloading anything."""
    config = _load_config_or_exit(config_path, server, username, password)
    client = XtreamClient(config.account.server, config.account.username, config.account.password)
    try:
        series: SeriesInfo = client.get_series_info(series_id)
    except XcVodDlError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)

    gap_map = detect_gaps_in_series(series)
    dupe_map = detect_duplicate_episodes_in_series(series)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "gaps": {str(season): missing for season, missing in gap_map.items()},
                    "duplicates": {str(season): repeated for season, repeated in dupe_map.items()},
                }
            )
        )
    else:
        click.echo(format_gap_report(series.name, gap_map, dupe_map))
    sys.exit(0 if not gap_map and not dupe_map else 1)


@main.command()
@click.option("--server", help="Overrides the configured server URL.")
@click.option("--username", help="Overrides the configured username.")
@click.option("--password", help="Overrides the configured password.")
@click.option(
    "--config", "config_path", type=click.Path(path_type=Path), help="Path to config.toml."
)
@click.option("--serial", is_flag=True, help="Force one-at-a-time downloads.")
@click.option(
    "--parallel", "parallel_override", type=int, help="Force a specific parallel download count."
)
@click.option("--verify-mode", type=click.Choice(["quick", "full"]), help="Override verify mode.")
@click.option(
    "--state-db", "state_db_override", type=click.Path(path_type=Path), help="Override state.db path."
)
@click.option("--quiet", is_flag=True, help="Only log warnings/errors.")
def resume(
    server: str | None,
    username: str | None,
    password: str | None,
    config_path: Path | None,
    serial: bool,
    parallel_override: int | None,
    verify_mode: str | None,
    state_db_override: Path | None,
    quiet: bool,
) -> None:
    """Retry pending/failed/in-progress items left over in state.db from a
    previous run, without re-browsing or re-querying the catalog.

    state.db can be shared across multiple download directories (e.g. a
    `state_db` configured to an absolute, non-project-local path). A "done"
    record made that way is only true for the directory it was fetched
    into — if the same episode/movie was never actually downloaded *here*,
    trusting the database alone would silently skip it. So every "done"
    record is checked against this directory's disk before being excluded;
    anything missing gets reset to pending and resumed like any other
    incomplete item, rather than resume reporting "nothing to resume" while
    files are actually missing.
    """
    config = _load_config_or_exit(config_path, server, username, password)
    _setup_logging(config, quiet)
    resolved_verify_mode = verify_mode or config.download.verify_mode
    state_path = state_db_override or config.download.state_db
    if not state_path.exists():
        click.echo(f"no state.db found at {state_path} — nothing to resume")
        return

    client = XtreamClient(config.account.server, config.account.username, config.account.password)
    session = requests.Session()

    with StateStore(state_path) as state:
        stale_done = [r for r in state.list_by_status("done") if not Path(r.target_path).exists()]
        for record in stale_done:
            state.mark_status(
                record.id,
                "pending",
                bytes_downloaded=0,
                last_error="was marked done, but the file is missing in this directory",
            )
        if stale_done:
            click.echo(
                f"{len(stale_done)} item(s) were marked done elsewhere but are missing here — re-queuing"
            )
            logger.info(
                "%d item(s) reset from done to pending: missing on disk here",
                len(stale_done),
            )

        incomplete = state.list_incomplete()
        if not incomplete:
            click.echo("nothing to resume")
            return

        jobs = []
        for record in incomplete:
            kind, raw_id = record.id.split(":", 1)
            ext = record.container_extension or "mp4"
            url = (
                client.movie_url(int(raw_id), ext)
                if kind == "movie"
                else client.episode_url(int(raw_id), ext)
            )
            jobs.append(
                DownloadJob(
                    id=record.id,
                    url=url,
                    target_path=Path(record.target_path),
                    kind=kind,
                    title=record.title,
                    series_id=record.series_id,
                    season=record.season,
                    episode_num=record.episode_num,
                    container_extension=record.container_extension,
                )
            )

        click.echo(f"resuming {len(jobs)} item(s)")
        results = _run_pipeline(
            client,
            config,
            session,
            jobs,
            state,
            serial=serial,
            parallel_override=parallel_override,
            verify_mode=resolved_verify_mode,
            quiet=quiet,
        )

    succeeded = sum(1 for ok in results.values() if ok)
    failed = len(results) - succeeded
    click.echo(f"done: {succeeded} succeeded, {failed} failed")
    logger.info("done: %d succeeded, %d failed", succeeded, failed)
    sys.exit(0 if failed == 0 else 1)


@main.command()
@click.option("--server", help="Overrides the configured server URL.")
@click.option("--username", help="Overrides the configured username.")
@click.option("--password", help="Overrides the configured password.")
@click.option(
    "--config", "config_path", type=click.Path(path_type=Path), help="Path to config.toml."
)
@click.option(
    "--state-db", "state_db_override", type=click.Path(path_type=Path), help="Override state.db path."
)
@click.option(
    "--status",
    "status_filter",
    type=click.Choice(["pending", "downloading", "verifying", "failed", "done"]),
    help="Only show items with this status.",
)
@click.option("--all", "show_all", is_flag=True, help="Also list every 'done' item, not just a count.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def status(
    server: str | None,
    username: str | None,
    password: str | None,
    config_path: Path | None,
    state_db_override: Path | None,
    status_filter: str | None,
    show_all: bool,
    as_json: bool,
) -> None:
    """Show what's tracked in state.db right now — pending, in progress,
    failed, or done — without resuming or re-running anything. What
    `resume` would act on is exactly the pending/downloading/verifying/
    failed groups shown here."""
    config = _load_config_or_exit(config_path, server, username, password)
    state_path = state_db_override or config.download.state_db
    if not state_path.exists():
        click.echo(f"no state.db found at {state_path}")
        return

    with StateStore(state_path) as state:
        records = state.list_by_status(status_filter) if status_filter else state.list_all()

    if as_json:
        click.echo(
            json.dumps(
                [
                    {
                        "id": r.id,
                        "kind": r.kind,
                        "title": r.title,
                        "status": r.status,
                        "season": r.season,
                        "episode_num": r.episode_num,
                        "bytes_downloaded": r.bytes_downloaded,
                        "attempts": r.attempts,
                        "last_error": r.last_error,
                        "updated_at": r.updated_at,
                    }
                    for r in records
                ]
            )
        )
        return

    if not records:
        click.echo("nothing tracked in state.db")
        return

    if status_filter:
        _print_status_group(status_filter, records)
        return

    by_status: dict[str, list] = {}
    for r in records:
        by_status.setdefault(r.status, []).append(r)

    for name in ("downloading", "verifying", "pending", "failed"):
        group = by_status.get(name, [])
        if group:
            _print_status_group(name, group)

    done = by_status.get("done", [])
    if show_all:
        _print_status_group("done", done)
    elif done:
        click.echo(f"done: {len(done)} item(s)")


def _print_status_group(status_name: str, records: list) -> None:
    click.echo(f"{status_name} ({len(records)}):")
    for r in records:
        line = f"  {r.id}  {r.title}"
        if r.status == "failed" and r.last_error:
            line += f"  ({r.last_error})"
        click.echo(line)


@main.command()
@click.option(
    "--path",
    "root",
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    default=Path("."),
    help="Directory to scan for stray .voddl files.",
)
@click.option("-y", "--yes", is_flag=True, help="Don't ask for confirmation before deleting.")
def clean(root: Path, yes: bool) -> None:
    """Remove stray .voddl files — unverified partial downloads that are safe to discard."""
    stray = sorted(root.rglob("*.voddl"))
    if not stray:
        click.echo("nothing to clean")
        return

    click.echo(f"found {len(stray)} incomplete file(s):")
    for path in stray:
        click.echo(f"  {path}")

    if not yes and not click.confirm("Delete these?", default=False):
        return

    for path in stray:
        path.unlink()
    click.echo(f"removed {len(stray)} file(s)")


@main.command()
@click.option("--server", help="Overrides the configured server URL.")
@click.option("--username", help="Overrides the configured username.")
@click.option("--password", help="Overrides the configured password.")
@click.option(
    "--config", "config_path", type=click.Path(path_type=Path), help="Path to config.toml."
)
@click.option("--host", default="127.0.0.1", help="Interface to bind the web UI to.")
@click.option("--port", default=8787, type=int, help="Port to bind the web UI to.")
def serve(
    server: str | None,
    username: str | None,
    password: str | None,
    config_path: Path | None,
    host: str,
    port: int,
) -> None:
    """Launch a local web UI (search/select/download) driving the same
    download engine as fetch/browse — for anyone who'd rather click than
    type. Needs the optional web extra: pip install xc-vod-dl[web]."""
    config = _load_config_or_exit(config_path, server, username, password)
    try:
        import uvicorn

        from xc_vod_dl.web.app import create_app
    except ImportError:
        click.echo(
            "error: the web UI needs extra dependencies — install with: pip install xc-vod-dl[web]",
            err=True,
        )
        sys.exit(2)

    _setup_logging(config, quiet=False)
    app = create_app(config)
    click.echo(f"xc-vod-dl web UI on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
