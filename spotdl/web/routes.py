"""
Module which contains the web client routes and functions.
"""

import asyncio
import re
import uuid
from pathlib import Path
from typing import Any, List, Optional, cast

import requests
# from datastar_py.sse import DatastarEvent
from datastar_py.fastapi import ReadSignals
from datastar_py.fastapi import (
    ServerSentEventGenerator as SSE,  # DatastarResponse,; read_signals,
)
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from spotdl._version import __version__
from spotdl.download.downloader import AUDIO_PROVIDERS, LYRICS_PROVIDERS
from spotdl.types.album import Album
from spotdl.types.artist import Artist
from spotdl.types.playlist import Playlist
from spotdl.types.song import Song
from spotdl.utils.config import get_spotdl_path
from spotdl.utils.ffmpeg import FFMPEG_FORMATS
from spotdl.utils.search import get_search_results, get_simple_songs
from spotdl.utils.spotify import SpotifyClient
from spotdl.utils.web import Client, app_state
from spotdl.web.utils import Signals, datastar_response, handle_signals

__all__ = ["router"]

router = APIRouter()

templates = Jinja2Templates(directory=Path(__file__).parent / "components")


# PATHS


SPOTIFY_URL_RE = re.compile(
    r"(?:https?://)?open\.spotify\.com/(?:intl-[^/\s]+/)?(track|album|playlist|artist)/([A-Za-z0-9]+)",
    re.IGNORECASE,
)
SPOTIFY_URI_RE = re.compile(
    r"spotify:(track|album|playlist|artist):([A-Za-z0-9]+)",
    re.IGNORECASE,
)
SPOTIFY_SHORT_URL_RE = re.compile(
    r"https?://spotify\.link/[A-Za-z0-9]+",
    re.IGNORECASE,
)


def _fast_artist_songs(artist_url: str) -> List[Song]:
    """
    Fast artist song loader for the web UI: fetches the artist's top tracks and
    primary releases concurrently without crawling dozens of albums sequentially.
    """
    spotify_client = SpotifyClient()
    if hasattr(spotify_client, "raw_artist_union"):
        artist_union = spotify_client.raw_artist_union(artist_url)
        artist_name = (
            artist_union.get("profile", {}).get("name")
            or artist_union.get("name")
            or ""
        )
        songs: List[Song] = []
        seen_ids = set()

        # 1. Extract topTracks immediately from the single artistUnion response
        top_items = (
            artist_union.get("discography", {})
            .get("topTracks", {})
            .get("items", [])
        )
        for item in top_items:
            track = item.get("track") if isinstance(item, dict) else None
            if not isinstance(track, dict):
                continue
            tid = track.get("id") or (track.get("uri") or "").split(":")[-1]
            if not tid or tid in seen_ids:
                continue
            seen_ids.add(tid)
            artists = [
                a.get("profile", {}).get("name") or a.get("name") or artist_name
                for a in track.get("artists", {}).get("items", [])
                if isinstance(a, dict)
            ] or [artist_name]
            album_of_track = track.get("albumOfTrack") or {}
            sources = album_of_track.get("coverArt", {}).get("sources") or []
            cover_url = (
                max(
                    sources,
                    key=lambda s: (s.get("width") or 0) * (s.get("height") or 0),
                ).get("url")
                if sources
                else None
            )
            dur_ms = (
                track.get("duration", {}).get("totalMilliseconds")
                or track.get("duration_ms")
                or 0
            )
            explicit = (
                track.get("contentRating", {}).get("label") == "EXPLICIT"
                or bool(track.get("explicit"))
            )
            songs.append(
                Song.from_missing_data(
                    name=track.get("name", ""),
                    artists=artists,
                    artist=artists[0],
                    album_name=album_of_track.get("name", ""),
                    album_artist=artists[0],
                    duration=int(dur_ms / 1000),
                    song_id=tid,
                    explicit=explicit,
                    url=f"https://open.spotify.com/track/{tid}",
                    cover_url=cover_url,
                )
            )

        # 2. Enrich with search results for this artist so the user gets up to 100 tracks fast
        if artist_name:
            try:
                for s in get_search_results(artist_name):
                    if s.song_id not in seen_ids and (
                        artist_name.lower() in s.artist.lower()
                        or any(artist_name.lower() in a.lower() for a in s.artists)
                    ):
                        seen_ids.add(s.song_id)
                        songs.append(s)
            except Exception:
                pass

        if songs:
            return songs

    return Artist.from_url(artist_url, fetch_songs=False).songs


def resolve_search_query(raw_query: str) -> List[Song]:
    """
    Resolve any search query or Spotify/YouTube link into a list of Song objects.
    """
    query = (raw_query or "").strip()
    if not query:
        return []

    # Resolve spotify.link short URLs
    short_match = SPOTIFY_SHORT_URL_RE.search(query)
    if short_match:
        try:
            resp = requests.head(
                short_match.group(0), allow_redirects=True, timeout=10
            )
            query = resp.url
        except Exception:
            pass

    # Extract Spotify URI if present
    uri_match = SPOTIFY_URI_RE.search(query)
    if uri_match:
        kind, item_id = uri_match.group(1).lower(), uri_match.group(2)
        query = f"https://open.spotify.com/{kind}/{item_id}"

    # Extract Spotify URL (including /intl-es/ or surrounding text)
    url_match = SPOTIFY_URL_RE.search(query)
    if url_match:
        kind, item_id = url_match.group(1).lower(), url_match.group(2)
        clean_url = f"https://open.spotify.com/{kind}/{item_id}"
        try:
            if kind == "track":
                return [Song.from_url(clean_url)]
            if kind == "album":
                return Album.from_url(clean_url, fetch_songs=False).songs
            if kind == "playlist":
                return Playlist.from_url(clean_url, fetch_songs=False).songs
            if kind == "artist":
                return _fast_artist_songs(clean_url)
        except Exception as exc:
            app_state.logger.error(f"Error resolving Spotify URL {clean_url}: {exc}")
            return []

    # Handle YouTube URLs or prefixed queries (artist:, album:, playlist:)
    if query.startswith(("artist:", "album:", "playlist:")) or any(
        domain in query
        for domain in ("youtube.com/", "youtu.be/", "soundcloud.com/", "bandcamp.com/")
    ):
        try:
            if query.startswith("artist:"):
                artist_name = query.split(":", 1)[1].strip()
                return get_search_results(artist_name)
            return get_simple_songs([query])
        except Exception as exc:
            app_state.logger.error(f"Error resolving query {query}: {exc}")
            return []

    try:
        return get_search_results(query)
    except Exception as exc:
        app_state.logger.error(f"Error searching '{query}': {exc}")
        return []


@router.get("/")
async def home(request: Request):
    """
    Handle the home page request.
    """
    return templates.TemplateResponse(
        request=request,
        name="home.html.j2",
        context={"__version__": __version__},
    )


@router.get("/search")
async def search(q: Optional[str] = None, request: Request = None):  # type: ignore
    """
    Handle the search input and render results directly on initial page load.
    """
    search_term = (q or "").strip()
    songs: List[Song] = []
    if search_term:
        songs = await asyncio.to_thread(resolve_search_query, search_term)

    return templates.TemplateResponse(
        request=request,
        name="search.html.j2",
        context={
            "__version__": __version__,
            "search_term": search_term,
            "songs": songs,
        },
    )


@router.get("/downloads")
async def downloads(request: Request):
    """
    Handle the downloads page request.
    """
    return templates.TemplateResponse(
        request=request,
        name="downloads.html.j2",
        context={
            "__version__": __version__,
        },
    )


# ACTIONS


@router.get("/client/load")
@datastar_response
async def handle_get_client_load(datastar_signals: ReadSignals):
    """
    Handle the loading of the client.
    """
    app_state.logger.info("Loading client...")
    signals = handle_signals(datastar_signals)
    if not signals.client_id:
        # Generate a new client ID if not provided
        app_state.logger.warning("No client ID provided, generating a new one.")
        signals.client_id = uuid.uuid4().hex
        client = Client(signals.client_id)
    else:
        found_client = Client.get_instance(signals.client_id)
        if found_client is None:
            # Create a new client if not found
            app_state.logger.warning(
                f"Client {signals.client_id} not found, creating new client..."
            )
            signals.client_id = uuid.uuid4().hex
            client = Client(signals.client_id)
        else:
            client = found_client
    await client.connect()

    # First send the client ID and then the home template.
    yield SSE.patch_elements("""<div id="status"></div>""")
    # Send the client ID to the client
    yield SSE.patch_signals(
        {
            "client_id": client.client_id,
        }
    )
    try:
        while True:
            yield SSE.patch_elements(f"""<div id="overall-completed-tasks">
                {len(client.downloader.progress_handler.progress_tracker.songs)}
                </div>""")
            await asyncio.sleep(1)
    finally:
        app_state.logger.info(f"[{signals.client_id}] Unloading client...")
        await client.disconnect()


@router.get("/client/search")
@datastar_response
async def handle_get_client_search(
    datastar_signals: ReadSignals, q: Optional[str] = None
):
    """
    Handle the search input.
    """
    app_state.logger.info("Loading search...")
    signals = handle_signals(datastar_signals)
    search_term = (q or signals.search_term or "").strip()
    signals.search_term = search_term
    app_state.logger.info(f"[{signals.client_id}] Search term: {search_term}")

    songs = await asyncio.to_thread(resolve_search_query, search_term)

    yield SSE.patch_elements(
        templates.get_template("search-list.html.j2").render(
            songs=songs,
        )
    )


@router.get("/client/downloads")
@datastar_response
async def handle_get_client_downloads(datastar_signals: ReadSignals):
    """
    Handle the retrieval of client downloads.
    """
    app_state.logger.info("Loading downloads...")
    signals = handle_signals(datastar_signals)
    app_state.logger.info(f"[{signals.client_id}] Downloads requested.")
    client = Client.get_instance(signals.client_id)
    if client is None:
        app_state.logger.warning(
            f"[{signals.client_id}] Client not found, cannot load downloads."
        )
        yield SSE.patch_elements(
            templates.get_template("status-disconnected.html.j2").render()
        )
        return
    while True:
        client_song_downloads = (
            client.downloader.progress_handler.progress_tracker.songs
        )
        yield SSE.patch_elements(
            templates.get_template("download-list.html.j2").render(
                client_song_downloads=client_song_downloads.values()
            )
        )
        await asyncio.sleep(1)


@router.get("/client/settings")
@datastar_response
async def handle_get_client_settings(datastar_signals: ReadSignals):
    """
    Handle the retrieval of client settings.
    """
    signals = handle_signals(datastar_signals)
    client = Client.get_instance(signals.client_id)
    if client is None:
        app_state.logger.warning(
            f"Client {signals.client_id} not found, cannot update settings."
        )
        yield SSE.patch_elements(
            templates.get_template("status-disconnected.html.j2").render()
        )
        return
    app_state.logger.info(f"[{signals.client_id}] Sending client settings...")
    yield SSE.patch_signals(
        {
            "downloader_settings": cast(Any, client.downloader_settings),
        }
    )


@router.post("/client/settings")
@datastar_response
async def handle_post_client_settings(datastar_signals: ReadSignals):
    """
    Handle the update of client settings.
    """
    signals = handle_signals(datastar_signals)
    client = Client.get_instance(signals.client_id)
    error = None
    if client is not None and signals.downloader_settings is not None:
        app_state.logger.info(f"[{signals.client_id}] Updating settings...")
        try:
            client.update_settings(signals.downloader_settings)
        except ValueError as exception:
            error = str(exception)
            app_state.logger.warning(
                f"[{signals.client_id}] Invalid settings, not saved: {error}"
            )

    if client is not None and error is None:
        yield SSE.patch_elements("""
                <div id="settings-status">
                    <div id="settings-is-saved" class="alert alert-success shadow-lg">
                        <div>
                            <svg 
                            xmlns="http://www.w3.org/2000/svg" 
                            class="stroke-current flex-shrink-0 h-6 w-6" 
                            fill="none"
                            viewBox="0 0 24 24">
                                <path 
                                stroke-linecap="round" 
                                stroke-linejoin="round" 
                                stroke-width="2"
                                d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                            </svg>
                            <span>Changes saved</span>
                        </div>
                    </div>
                </div>
            """)
        yield SSE.patch_signals(
            {
                "downloader_settings": cast(Any, client.downloader_settings),
            }
        )
    else:
        if client is None:
            app_state.logger.warning(
                f"[{signals.client_id}] Client not found, cannot update settings."
            )
            yield SSE.patch_elements(
                templates.get_template("status-disconnected.html.j2").render()
            )
        else:
            # Put the settings shown in the UI back to the saved ones
            yield SSE.patch_signals(
                {
                    "downloader_settings": cast(Any, client.downloader_settings),
                }
            )
        yield SSE.patch_elements("""
                <div id="settings-status">
                    <div id="settings-is-not-saved" class="alert alert-error shadow-lg">
                        <div>
                            <svg 
                            xmlns="http://www.w3.org/2000/svg" 
                            class="stroke-current 
                            flex-shrink-0 h-6 w-6" 
                            fill="none"
                            viewBox="0 0 24 24">
                                <path 
                                stroke-linecap="round" 
                                stroke-linejoin="round" 
                                stroke-width="2"
                d="M10 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2m7-2a9 9 0 11-18 0 9 9 0 0118 0z" 
                            />
                            </svg>
                            <span>Error! Unable to save settings</span>
                        </div>
                    </div>
                </div>
            """)
    #  sleep for 3 seconds then clear the status message
    await asyncio.sleep(3)
    yield SSE.patch_elements("""
            <div id="settings-status">
            </div>
        """)


@router.post("/client/download/")
@datastar_response
async def handle_post_client_download(datastar_signals: ReadSignals):
    """
    Handle the download request from the client.
    """
    signals = handle_signals(datastar_signals)
    async for update in gen_download(signals):
        yield update


# HELPERS


async def gen_download(signals: Signals):
    """
    Generate the download process for the client.
    """
    client = Client.get_instance(signals.client_id)
    if client is None:
        app_state.logger.warning(
            f"Client {signals.client_id} not found, cannot load downloads."
        )
        yield SSE.patch_elements(
            templates.get_template("status-disconnected.html.j2").render()
        )
        return
    app_state.logger.info(
        f"[{signals.client_id}] Download requested: {signals.song_url}"
    )
    yield SSE.patch_elements(f"""
            <button id="download-{signals.song_url}" class="btn btn-primary btn-square loading">
                </button>
        """)

    # Use the server's output setting, clients can't choose where files go
    if app_state.web_settings.get("web_use_output_dir", False):
        client.downloader.settings["output"] = app_state.downloader_settings["output"]
    else:
        client.downloader.settings["output"] = str(
            (get_spotdl_path() / f"web/sessions/{client.client_id}").absolute()
        )

    try:
        # Fetch song metadata
        song = await asyncio.to_thread(Song.from_url, signals.song_url)
        app_state.logger.info(f"Downloading song: {song}")

        # Download Song
        _, path = await client.downloader.pool_download(song)
        yield SSE.patch_elements(f"""
            <button id="download-{signals.song_url}" class="btn btn-primary btn-square">
                    <iconify-icon icon="clarity:check-line" style="font-size: 24px"></iconify-icon>
                </button>
        """)

        if path is None:
            app_state.logger.error(f"Failure downloading {song.name}")

        # return str(path.absolute())

    except Exception as exception:
        app_state.logger.error(f"Error downloading! {exception}")


# COMPONENTS


@router.get("/client/component/settings-content")
@datastar_response
async def handle_get_client_component_settings(datastar_signals: ReadSignals):
    """
    Handle the request for the client settings component.
    """
    signals = handle_signals(datastar_signals)
    client = Client.get_instance(signals.client_id)
    if client is None:
        app_state.logger.warning(
            f"Client {signals.client_id} not found, cannot update settings."
        )
        yield SSE.patch_elements(
            templates.get_template("status-disconnected.html.j2").render()
        )
        return
    app_state.logger.info(f"[{signals.client_id}] Loading settings view...")
    # clear state
    yield SSE.patch_elements("""<div id="component-settings-content"></div>""")
    # render the settings content
    yield SSE.patch_elements(
        templates.get_template("settings-content.html.j2").render(
            downloader_settings=client.downloader_settings,
            AUDIO_PROVIDERS=AUDIO_PROVIDERS,
            LYRICS_PROVIDERS=LYRICS_PROVIDERS,
            FORMATS=FFMPEG_FORMATS.keys(),
        )
    )
    # spotify_client = SpotifyClient()
    # print(f"{spotify_client = }")
    yield SSE.patch_signals(
        {
            "downloader_settings": cast(Any, client.downloader_settings),
            # "spotify_settings": {
            #     "client_id": spotify_client.credential_manager.client_id
            # },
        }
    )


@router.get("/client/component/search-input-rotating-placeholder")
@datastar_response
async def handle_client_component_search_input_rotating_placeholder():
    """
    Handle the search input rotating placeholder component.
    """
    app_state.logger.info("Loading rotating-placeholder...")
    placeholder_items = [
        "All Eyes On Me - Bo Burnham",
        "https://open.spotify.com/track/4vfN00PlILRXy5dcXHQE9M?si=e4d9e7c044dd4a8f",
        "Lil Wayne",
        "Drive - Miley Cyrus",
        "Sofia - TMG",
        "Lightning Crashes - Live",
    ]
    index = 0
    while True:
        t = templates.get_template("search-input-rotating-placeholder.html.j2").render(
            placeholder_item=placeholder_items[index]
        )
        yield SSE.patch_elements(t)
        await asyncio.sleep(5)
        index += 1
        if index >= len(placeholder_items):
            index = 0
