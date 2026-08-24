from __future__ import annotations

import xml.etree.ElementTree as ET

from xc_vod_dl.api.models import Episode, SeriesInfo, VodInfo


def _render(root: ET.Element) -> str:
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


def _add_uniqueid(parent: ET.Element, tmdb_id: str | None) -> None:
    if tmdb_id:
        uid = ET.SubElement(parent, "uniqueid", type="tmdb", default="true")
        uid.text = tmdb_id


def build_movie_nfo(vod: VodInfo) -> str:
    """Kodi/Jellyfin/Plex-compatible <movie> NFO."""
    root = ET.Element("movie")
    ET.SubElement(root, "title").text = vod.name
    ET.SubElement(root, "plot").text = vod.plot
    if vod.genre:
        ET.SubElement(root, "genre").text = vod.genre
    if vod.release_date:
        ET.SubElement(root, "premiered").text = vod.release_date
    _add_uniqueid(root, vod.tmdb_id)
    return _render(root)


def build_episode_nfo(episode: Episode, series_name: str) -> str:
    """Kodi/Jellyfin/Plex-compatible <episodedetails> NFO."""
    root = ET.Element("episodedetails")
    ET.SubElement(root, "title").text = episode.title or f"Episode {episode.episode_num}"
    ET.SubElement(root, "showtitle").text = series_name
    ET.SubElement(root, "season").text = str(episode.season)
    ET.SubElement(root, "episode").text = str(episode.episode_num)
    if episode.plot:
        ET.SubElement(root, "plot").text = episode.plot
    _add_uniqueid(root, episode.tmdb_id)
    return _render(root)


def build_series_nfo(series: SeriesInfo) -> str:
    """Kodi/Jellyfin/Plex-compatible <tvshow> NFO."""
    root = ET.Element("tvshow")
    ET.SubElement(root, "title").text = series.name
    ET.SubElement(root, "plot").text = series.plot
    if series.genre:
        ET.SubElement(root, "genre").text = series.genre
    _add_uniqueid(root, series.tmdb_id)
    return _render(root)
