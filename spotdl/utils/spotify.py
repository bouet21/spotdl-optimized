"""
Module for interacting with Spotify API.
To use this module, you must have a Spotify API key and Spotify API secret.

```python
import spotdl.utils.spotify
spotify.Spotify.init(client_id, client_secret)
```
"""

import json
import logging
import re
import threading
import time
from typing import Any, Dict, Optional

import requests
import spotapi  # type: ignore
from spotipy import Spotify
from spotipy.cache_handler import CacheFileHandler, MemoryCacheHandler
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth
from SpotipyFree import Spotify as FreeSpotify
from SpotipyFree.Formatter import SpotifyFormatter

from spotdl.utils.config import get_cache_path, get_spotdl_path, get_spotify_cache_path

__all__ = [
    "SpotifyError",
    "SpotifyClient",
    "save_spotify_cache",
]

logger = logging.getLogger(__name__)

OFFICIAL_API_ONLY_OPTIONS = {
    "auth_token": "--auth-token",
    "user_auth": "--user-auth",
    "use_cache_file": "--use-cache-file",
}


class SpotifyError(Exception):
    """
    Base class for all exceptions related to SpotifyClient.
    """


class _OfficialSpotifyClient(Spotify):
    """
    Spotipy-backed Spotify client used when the official API is requested.
    """

    cache: Dict[str, Optional[Dict]] = {}

    @classmethod
    def init(
        cls,
        client_id: str,
        client_secret: str,
        user_auth: bool = False,
        no_cache: bool = False,
        headless: bool = False,
        max_retries: int = 3,
        use_cache_file: bool = False,
        auth_token: Optional[str] = None,
        cache_path: Optional[str] = None,
    ) -> "_OfficialSpotifyClient":
        """
        Initializes the official SpotifyClient implementation.
        """

        credential_manager = None

        cache_handler = (
            CacheFileHandler(cache_path or get_cache_path())
            if not no_cache
            else MemoryCacheHandler()
        )
        if user_auth:
            credential_manager = SpotifyOAuth(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri="http://127.0.0.1:9900/",
                scope="user-library-read,user-follow-read,playlist-read-private",
                cache_handler=cache_handler,
                open_browser=not headless,
            )
        else:
            credential_manager = SpotifyClientCredentials(
                client_id=client_id,
                client_secret=client_secret,
                cache_handler=cache_handler,
            )

        if auth_token is not None:
            credential_manager = None

        client = cls(
            auth=auth_token,
            auth_manager=credential_manager,
            status_forcelist=(429, 500, 502, 503, 504, 404),
        )
        client.user_auth = user_auth
        client.no_cache = no_cache
        client.max_retries = max_retries
        client.use_cache_file = use_cache_file

        cache_file_loc = get_spotify_cache_path()
        if use_cache_file and cache_file_loc.exists():
            with open(cache_file_loc, "r", encoding="utf-8") as cache_file:
                client.cache = json.load(cache_file)
        elif use_cache_file:
            with open(cache_file_loc, "w", encoding="utf-8") as cache_file:
                json.dump(client.cache, cache_file)

        return client

    def _get(self, url, args=None, payload=None, **kwargs):
        """
        Overrides the get method of the SpotifyClient.
        Allows us to cache requests.
        """

        use_cache = not self.no_cache  # type: ignore # pylint: disable=E1101

        if args:
            kwargs.update(args)

        cache_key = None
        if use_cache:
            key_obj = dict(kwargs)
            key_obj["url"] = url
            key_obj["data"] = json.dumps(payload)
            cache_key = json.dumps(key_obj)
            if cache_key is None:
                cache_key = url
            if self.cache.get(cache_key) is not None:
                return self.cache[cache_key]

        response = None
        retries = self.max_retries  # type: ignore # pylint: disable=E1101
        while response is None:
            try:
                response = self._internal_call("GET", url, payload, kwargs)
            except (requests.exceptions.Timeout, requests.ConnectionError) as exc:
                retries -= 1
                if retries <= 0:
                    raise exc

        if use_cache and cache_key is not None:
            self.cache[cache_key] = response

        return response


def _init_official_spotify_client(**kwargs) -> _OfficialSpotifyClient:
    """
    Initialize the official Spotipy client.
    """

    return _OfficialSpotifyClient.init(**kwargs)


def _patch_spotapi_for_speed() -> None:
    """
    Patch spotapi.client.BaseClient so session tokens and GraphQL persistedQuery
    hashes are shared across all instances and persisted on disk instead of
    re-downloading ~15 MB of Spotify web-player JS bundles on every API call.
    """
    bc_cls = spotapi.client.BaseClient
    if getattr(bc_cls, "_spotdl_fast_patched", False):
        return

    undef = spotapi.client._Undefined
    cache_file = get_spotdl_path() / "spotapi_session_cache.json"
    lock = threading.RLock()
    hash_map: Dict[str, str] = {}
    shared_attrs = (
        "js_pack",
        "client_version",
        "access_token",
        "access_token_expires_at_ms",
        "client_token",
        "client_id",
        "device_id",
        "raw_hashes",
    )

    # Load persisted hashes/tokens from disk if still fresh
    try:
        if cache_file.exists():
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(data.get("hashes"), dict):
                hash_map.update(data["hashes"])
            now_ms = time.time() * 1000
            exp_ms = float(data.get("access_token_expires_at_ms") or 0)
            if exp_ms > now_ms + 60_000:
                for k in (
                    "js_pack",
                    "client_version",
                    "access_token",
                    "access_token_expires_at_ms",
                    "client_token",
                    "client_id",
                    "device_id",
                ):
                    if data.get(k):
                        setattr(bc_cls, k, data[k])
    except Exception:
        pass

    def _save_disk_cache() -> None:
        try:
            payload = {
                "hashes": hash_map,
                "js_pack": (
                    bc_cls.js_pack if bc_cls.js_pack is not undef else None
                ),
                "client_version": (
                    bc_cls.client_version
                    if bc_cls.client_version is not undef
                    else None
                ),
                "access_token": (
                    bc_cls.access_token
                    if bc_cls.access_token is not undef
                    else None
                ),
                "access_token_expires_at_ms": bc_cls.access_token_expires_at_ms,
                "client_token": (
                    bc_cls.client_token
                    if bc_cls.client_token is not undef
                    else None
                ),
                "client_id": (
                    bc_cls.client_id if bc_cls.client_id is not undef else None
                ),
                "device_id": (
                    bc_cls.device_id if bc_cls.device_id is not undef else None
                ),
            }
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(payload), encoding="utf-8")
        except Exception:
            pass

    def _sync_to_class(instance: Any) -> None:
        for attr in shared_attrs:
            val = instance.__dict__.get(attr, undef)
            if val is not undef and val:
                setattr(bc_cls, attr, val)

    orig_get_session = bc_cls.get_session
    orig_get_client_token = bc_cls.get_client_token
    orig_get_auth_vars = bc_cls._get_auth_vars
    orig_get_sha256_hash = bc_cls.get_sha256_hash
    orig_part_hash = bc_cls.part_hash

    def fast_get_session(self: Any) -> None:
        with lock:
            orig_get_session(self)
            _sync_to_class(self)
            _save_disk_cache()

    def fast_get_client_token(self: Any) -> None:
        with lock:
            if (
                bc_cls.client_token is not undef
                and bc_cls.access_token is not undef
                and (
                    not bc_cls.access_token_expires_at_ms
                    or time.time() * 1000 + 30_000 < bc_cls.access_token_expires_at_ms
                )
            ):
                for attr in shared_attrs:
                    setattr(self, attr, getattr(bc_cls, attr))
                return
            orig_get_client_token(self)
            _sync_to_class(self)
            _save_disk_cache()

    def fast_get_auth_vars(self: Any) -> None:
        with lock:
            bc_cls.access_token = undef
            self.access_token = undef
            orig_get_auth_vars(self)
            _sync_to_class(self)
            _save_disk_cache()

    def fast_get_sha256_hash(self: Any) -> None:
        with lock:
            if bc_cls.raw_hashes is not undef:
                self.raw_hashes = bc_cls.raw_hashes
                return
            orig_get_sha256_hash(self)
            _sync_to_class(self)
            for op_name, sha_val in re.findall(
                r'"([A-Za-z0-9_]+)","(?:query|mutation)","([0-9a-f]{64})"',
                str(self.raw_hashes),
            ):
                hash_map[op_name] = sha_val
            _save_disk_cache()

    def fast_part_hash(self: Any, name: str) -> str:
        cached = hash_map.get(name)
        if cached:
            return cached
        with lock:
            if name in hash_map:
                return hash_map[name]
            if bc_cls.raw_hashes is not undef:
                self.raw_hashes = bc_cls.raw_hashes
            h = orig_part_hash(self, name)
            _sync_to_class(self)
            hash_map[name] = h
            _save_disk_cache()
            return h

    bc_cls.get_session = fast_get_session
    bc_cls.get_client_token = fast_get_client_token
    bc_cls._get_auth_vars = fast_get_auth_vars
    bc_cls.get_sha256_hash = fast_get_sha256_hash
    bc_cls.part_hash = fast_part_hash
    bc_cls._spotdl_fast_patched = True


class _FastFreeSpotify(FreeSpotify):
    """
    Optimized SpotipyFree subclass with in-memory response caching and
    O(1) artist_albums / artist top-tracks extraction.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._mem_cache: Dict[str, Any] = {}
        self._cache_lock = threading.RLock()

    def _cached_call(self, key: str, fn, *args, **kwargs):
        with self._cache_lock:
            if key in self._mem_cache:
                return self._mem_cache[key]
        result = fn(*args, **kwargs)
        with self._cache_lock:
            if len(self._mem_cache) > 512:
                self._mem_cache.clear()
            self._mem_cache[key] = result
        return result

    def urlToId(self, url: str) -> str:
        clean = url.split("?")[0].rstrip("/")
        if "spotify:" in clean:
            return clean.split(":")[-1]
        return clean.split("/")[-1]

    def track(self, trackId, *args, **kwargs):
        tid = self.urlToId(trackId) if self.isUrl(trackId) else str(trackId)
        return self._cached_call(f"track:{tid}", super().track, tid, *args, **kwargs)

    def album(self, albumId, *args, **kwargs):
        aid = self.urlToId(albumId) if self.isUrl(albumId) else str(albumId)
        return self._cached_call(f"album:{aid}", super().album, aid, *args, **kwargs)

    def album_tracks(self, albumId, *args, **kwargs):
        aid = self.urlToId(albumId) if self.isUrl(albumId) else str(albumId)
        return self._cached_call(
            f"album_tracks:{aid}", super().album_tracks, aid, *args, **kwargs
        )

    def artist(self, artistId, *args, **kwargs):
        arid = self.urlToId(artistId) if self.isUrl(artistId) else str(artistId)
        return self._cached_call(
            f"artist:{arid}", super().artist, arid, *args, **kwargs
        )

    def raw_artist_union(self, artistId: str) -> Dict[str, Any]:
        arid = self.urlToId(artistId) if self.isUrl(artistId) else str(artistId)
        return self._cached_call(
            f"artist_union:{arid}",
            lambda i: spotapi.Artist().get_artist(i)["data"]["artistUnion"],
            arid,
        )

    def artist_albums(
        self, artistId, limit=-1, offset=0, include_groups="album", *args, **kwargs
    ):
        """
        Extract artist album list directly from artistUnion.discography without
        making N sequential self.album(id) requests.
        """
        arid = self.urlToId(artistId) if self.isUrl(artistId) else str(artistId)
        allowed = set(include_groups.split(","))
        artist_union = self.raw_artist_union(arid)
        discog = artist_union.get("discography", {})

        merged = []
        for key, albums_list in discog.items():
            norm_key = key.removesuffix("s")
            if norm_key not in allowed or not isinstance(albums_list, dict):
                continue
            for group in albums_list.get("items", []):
                for rel in group.get("releases", {}).get("items", []):
                    rel_id = rel.get("id")
                    if not rel_id:
                        continue
                    merged.append(
                        {
                            "id": rel_id,
                            "name": rel.get("name", ""),
                            "album_type": (rel.get("type") or norm_key).lower(),
                            "external_urls": {
                                "spotify": f"https://open.spotify.com/album/{rel_id}"
                            },
                        }
                    )

        return SpotifyFormatter.addChunkInfo(merged)

    def playlist(self, playlistId, limit=-1, offset=0, *args, **kwargs):
        pid = self.urlToId(playlistId) if self.isUrl(playlistId) else str(playlistId)
        return self._cached_call(
            f"playlist:{pid}", super().playlist, pid, limit, offset, *args, **kwargs
        )

    def playlist_items(self, playlistId, *args, **kwargs):
        pid = self.urlToId(playlistId) if self.isUrl(playlistId) else str(playlistId)
        return self._cached_call(
            f"playlist_items:{pid}", super().playlist_items, pid, *args, **kwargs
        )

    def search(self, query, type="track", *args, **kwargs):
        return self._cached_call(
            f"search:{type}:{query}", super().search, query, type, *args, **kwargs
        )


def _init_free_spotify_client(**kwargs) -> Any:
    """
    Initialize the default SpotipyFree client with speed optimizations.
    """
    _patch_spotapi_for_speed()
    client = _FastFreeSpotify(**kwargs)

    # Pre-warm session and hashes in a background thread
    def _warmup():
        try:
            bc = spotapi.client.BaseClient(spotapi.Public.song_search.__globals__["client_pool"].get())
            if bc.client_token is spotapi.client._Undefined:
                bc.get_client_token()
            bc.part_hash("searchDesktop")
        except Exception:
            pass

    threading.Thread(target=_warmup, daemon=True).start()
    return client


class SpotifyClient:
    """
    Runtime-selected Spotify client facade.
    """

    _instance: Optional[Any] = None
    _use_official_api = False

    def __new__(cls):
        """
        Return the initialized Spotify client implementation.
        """

        if cls._instance is None:
            raise SpotifyError(
                "Spotify client not created. Call SpotifyClient.init"
                "("
                "client_id, client_secret, user_auth=False, no_cache=False, "
                "headless=False, max_retries=3, use_cache_file=False, "
                "use_official_api=False, auth_token=None, cache_path=None"
                ") first."
            )

        return cls._instance

    def __getattr__(self, name: str) -> Any:
        """
        The selected backend provides Spotify API methods at runtime.
        """

        raise AttributeError(name)

    @classmethod
    def is_using_official_api(cls) -> bool:
        """
        Returns whether the active client uses the official Spotify Web API.
        """

        return cls._use_official_api

    @classmethod
    def init(
        cls,
        client_id: str,
        client_secret: str,
        user_auth: bool = False,
        no_cache: bool = False,
        headless: bool = False,
        max_retries: int = 3,
        use_cache_file: bool = False,
        use_official_api: bool = False,
        auth_token: Optional[str] = None,
        cache_path: Optional[str] = None,
    ) -> Any:
        """
        Initializes the selected SpotifyClient implementation.
        """

        if cls._instance is not None:
            raise SpotifyError("A spotify client has already been initialized")

        kwargs = {
            "client_id": client_id,
            "client_secret": client_secret,
            "user_auth": user_auth,
            "no_cache": no_cache,
            "headless": headless,
            "max_retries": max_retries,
            "use_cache_file": use_cache_file,
            "auth_token": auth_token,
            "cache_path": cache_path,
        }

        official_only_options = [
            option for key, option in OFFICIAL_API_ONLY_OPTIONS.items() if kwargs[key]
        ]
        if official_only_options and not use_official_api:
            logger.info(
                "Using the official Spotify Web API because %s %s requested.",
                ", ".join(official_only_options),
                "was" if len(official_only_options) == 1 else "were",
            )
            use_official_api = True

        if use_official_api:
            cls._instance = _init_official_spotify_client(**kwargs)
        else:
            cls._instance = _init_free_spotify_client(**kwargs)

        cls._use_official_api = use_official_api

        return cls._instance


def save_spotify_cache(cache: Dict[str, Optional[Dict]]):
    """
    Saves the Spotify cache to a file when the official API client is active.

    ### Arguments
    - cache: The cache to save.
    """

    if not SpotifyClient.is_using_official_api():
        return

    cache_file_loc = get_spotify_cache_path()

    logger.debug("Saving Spotify cache to %s", cache_file_loc)

    cache = {
        key: value
        for key, value in cache.items()
        if value is not None and "tracks/" in key
    }

    with open(cache_file_loc, "w", encoding="utf-8") as cache_file:
        json.dump(cache, cache_file)
