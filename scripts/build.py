import json
import os
import re
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit, urlunsplit


# ============================================================
# RUSSIA IPTV V7
#
# SOURCES
#   1. iptv-org API
#   2. iptv-russia
#   3. iptv-org Russian subdivision playlists
#
# QUALITY
#   ONLY actual ffprobe >= 1080p
#
# OUTPUT
#   output/russia.m3u
#   output/russia-best.m3u
#   output/russia-backup.m3u
#   output/russia-fhd.m3u
#   output/russia-4k.m3u
#   output/russia-hdr.m3u
#   output/regions/*.m3u
#   output/report.json
# ============================================================


API_BASE = "https://iptv-org.github.io/api"

IPTV_RUSSIA_M3U = (
    "https://raw.githubusercontent.com/"
    "substanc1/iptv-russia/main/streams/ru.m3u"
)

SUBDIVISION_BASE = (
    "https://iptv-org.github.io/iptv/subdivisions/"
)

OUTPUT_DIR = "output"
REGION_OUTPUT_DIR = os.path.join(
    OUTPUT_DIR,
    "regions",
)

MIN_HEIGHT = 1080

# GitHub Actions runner can handle this.
# Keep moderate because every URL is actually probed.
WORKERS = 24

FFPROBE_TIMEOUT = 8


# ============================================================
# HTTP
# ============================================================

def http_get(url, timeout=30):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Russia-IPTV-V7/1.0",
            "Accept": "*/*",
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=timeout,
    ) as response:
        return response.read()


def load_json(filename):
    url = f"{API_BASE}/{filename}"

    return json.loads(
        http_get(url).decode(
            "utf-8"
        )
    )


def load_text(url):
    return http_get(url).decode(
        "utf-8",
        errors="replace",
    )


# ============================================================
# SAFE HELPERS
# ============================================================

def safe_text(value):
    if value is None:
        return ""

    return str(value).strip()


def m3u_escape(value):
    return safe_text(value).replace(
        '"',
        "'",
    )


def normalize_url(url):
    """
    Do not reorder query parameters.
    Signed URLs can depend on exact ordering.
    """

    url = safe_text(url)

    if not url:
        return ""

    try:
        parsed = urlsplit(url)

        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.query,
                "",
            )
        )

    except Exception:
        return url.split(
            "#",
            1,
        )[0].strip()


def normalize_name(value):
    value = safe_text(value).lower()

    value = value.replace(
        "ё",
        "е",
    )

    value = re.sub(
        r"\b(?:uhd|4k|fhd|fullhd|hd|sd)\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"\s*\+\s*\d+\s*$",
        "",
        value,
    )

    value = re.sub(
        r"\[[^\]]*\]",
        " ",
        value,
    )

    value = re.sub(
        r"\([^)]*\)",
        " ",
        value,
    )

    value = re.sub(
        r"[^a-zа-я0-9]+",
        " ",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def detect_quality_label(text):
    text = safe_text(
        text
    ).lower()

    match = re.search(
        r"(\d{3,4})\s*[pi]",
        text,
    )

    if match:
        return (
            match.group(1)
            + "p"
        )

    if (
        "4k" in text
        or "uhd" in text
    ):
        return "2160p"

    if (
        "fhd" in text
        or "fullhd" in text
    ):
        return "1080p"

    if re.search(
        r"\bhd\b",
        text,
    ):
        return "hd"

    return ""


# ============================================================
# M3U PARSER
# ============================================================

EXTINF_RE = re.compile(
    r'^#EXTINF:(?P<duration>-?\d+)\s*'
    r'(?P<attrs>.*?),'
    r'(?P<title>.*)$'
)


def parse_m3u_attributes(text):
    result = {}

    pattern = re.compile(
        r'([A-Za-z0-9_-]+)="([^"]*)"'
    )

    for key, value in pattern.findall(
        text
    ):
        result[
            key.lower()
        ] = value

    return result


def parse_m3u(text):
    """
    Generic M3U parser.

    Supports:
      EXTINF
      EXTVLCOPT:http-referrer
      EXTVLCOPT:http-user-agent
    """

    entries = []

    current = None

    pending_referrer = ""
    pending_user_agent = ""

    for raw in text.splitlines():
        line = raw.strip()

        if not line:
            continue

        if line.startswith(
            "#EXTINF:"
        ):
            match = EXTINF_RE.match(
                line
            )

            if not match:
                current = None
                continue

            attrs = parse_m3u_attributes(
                match.group(
                    "attrs"
                )
            )

            title = safe_text(
                match.group(
                    "title"
                )
            )

            current = {
                "tvg_id": safe_text(
                    attrs.get(
                        "tvg-id"
                    )
                ),
                "tvg_name": safe_text(
                    attrs.get(
                        "tvg-name"
                    )
                    or title
                ),
                "tvg_logo": safe_text(
                    attrs.get(
                        "tvg-logo"
                    )
                ),
                "group": safe_text(
                    attrs.get(
                        "group-title"
                    )
                ),
                "title": title,
                "referrer": "",
                "user_agent": "",
            }

            pending_referrer = ""
            pending_user_agent = ""

            continue

        if line.startswith(
            "#EXTVLCOPT:http-referrer="
        ):
            pending_referrer = line.split(
                "=",
                1,
            )[1].strip()

            continue

        if line.startswith(
            "#EXTVLCOPT:http-user-agent="
        ):
            pending_user_agent = line.split(
                "=",
                1,
            )[1].strip()

            continue

        if line.startswith("#"):
            continue

        if (
            line.startswith(
                "http://"
            )
            or line.startswith(
                "https://"
            )
        ):
            if current:
                current["url"] = line
                current[
                    "referrer"
                ] = pending_referrer
                current[
                    "user_agent"
                ] = pending_user_agent

                entries.append(
                    current
                )

            current = None
            pending_referrer = ""
            pending_user_agent = ""

    return entries


# ============================================================
# CHANNEL INDEXES
# ============================================================

def build_channel_indexes(channels):
    by_id = {}
    by_name = {}

    for channel in channels:
        channel_id = safe_text(
            channel.get("id")
        )

        if not channel_id:
            continue

        country = safe_text(
            channel.get(
                "country"
            )
        ).upper()

        if country != "RU":
            continue

        by_id[channel_id] = channel

        names = []

        main_name = safe_text(
            channel.get("name")
        )

        if main_name:
            names.append(
                main_name
            )

        alt_names = (
            channel.get(
                "alt_names"
            )
            or []
        )

        if isinstance(
            alt_names,
            list,
        ):
            names.extend(
                safe_text(x)
                for x in alt_names
                if safe_text(x)
            )

        for name in names:
            normalized = normalize_name(
                name
            )

            if (
                normalized
                and normalized
                not in by_name
            ):
                by_name[
                    normalized
                ] = channel_id

    return (
        by_id,
        by_name,
    )


def find_channel(
    entry,
    channels_by_id,
    channels_by_name,
):
    tvg_id = safe_text(
        entry.get("tvg_id")
    )

    if tvg_id in channels_by_id:
        return tvg_id

    candidates = [
        entry.get("tvg_name"),
        entry.get("title"),
    ]

    for value in candidates:
        normalized = normalize_name(
            value
        )

        if not normalized:
            continue

        channel_id = (
            channels_by_name.get(
                normalized
            )
        )

        if channel_id:
            return channel_id

    return ""


# ============================================================
# CANDIDATE OBJECT
# ============================================================

def make_candidate(
    channel_id,
    url,
    source,
    title="",
    logo="",
    group="",
    referrer="",
    user_agent="",
    region_codes=None,
    region_names=None,
):
    return {
        "channel": channel_id,
        "url": normalize_url(
            url
        ),
        "source": source,
        "sources": [
            source
        ],
        "title": safe_text(
            title
        ),
        "logo": safe_text(
            logo
        ),
        "group": safe_text(
            group
        ),
        "referrer": safe_text(
            referrer
        ),
        "user_agent": safe_text(
            user_agent
        ),
        "regions": list(
            region_codes
            or []
        ),
        "region_names": list(
            region_names
            or []
        ),
        "declared_quality": detect_quality_label(
            title
        ),
    }


# ============================================================
# SOURCE 1 — IPTV-ORG API
# ============================================================

def build_api_candidates(
    streams,
    channels_by_id,
):
    result = []

    for stream in streams:
        channel_id = safe_text(
            stream.get(
                "channel"
            )
        )

        if (
            not channel_id
            or channel_id
            not in channels_by_id
        ):
            continue

        url = normalize_url(
            stream.get("url")
        )

        if not url:
            continue

        candidate = make_candidate(
            channel_id=channel_id,
            url=url,
            source="iptv-org",
            title=(
                stream.get(
                    "label"
                )
                or stream.get(
                    "quality"
                )
                or ""
            ),
            referrer=stream.get(
                "referrer"
            ),
            user_agent=stream.get(
                "user_agent"
            ),
        )

        candidate[
            "feed"
        ] = safe_text(
            stream.get(
                "feed"
            )
        )

        result.append(
            candidate
        )

    return result


# ============================================================
# SOURCE 2 — IPTV-RUSSIA
# ============================================================

def build_russia_candidates(
    entries,
    channels_by_id,
    channels_by_name,
):
    result = []
    unmatched = 0

    for entry in entries:
        url = normalize_url(
            entry.get("url")
        )

        if not url:
            continue

        channel_id = find_channel(
            entry,
            channels_by_id,
            channels_by_name,
        )

        if not channel_id:
            unmatched += 1
            continue

        result.append(
            make_candidate(
                channel_id=channel_id,
                url=url,
                source="iptv-russia",
                title=(
                    entry.get(
                        "title"
                    )
                    or entry.get(
                        "tvg_name"
                    )
                ),
                logo=entry.get(
                    "tvg_logo"
                ),
                group=entry.get(
                    "group"
                ),
                referrer=entry.get(
                    "referrer"
                ),
                user_agent=entry.get(
                    "user_agent"
                ),
            )
        )

    return (
        result,
        unmatched,
    )


# ============================================================
# RUSSIAN SUBDIVISIONS
# ============================================================

def build_subdivision_list(
    subdivisions
):
    result = []

    for item in subdivisions:
        country = safe_text(
            item.get(
                "country"
            )
        ).upper()

        code = safe_text(
            item.get(
                "code"
            )
        ).upper()

        name = safe_text(
            item.get(
                "name"
            )
        )

        if country != "RU":
            continue

        if not code.startswith(
            "RU-"
        ):
            continue

        if not name:
            continue

        result.append(
            {
                "code": code.lower(),
                "name": name,
                "url": (
                    SUBDIVISION_BASE
                    + code.lower()
                    + ".m3u"
                ),
            }
        )

    return sorted(
        result,
        key=lambda x: (
            x["name"].lower(),
            x["code"],
        ),
    )


def load_subdivision_playlists(
    subdivisions,
    channels_by_id,
    channels_by_name,
):
    all_candidates = []

    stats = {}

    failed = []

    for index, subdivision in enumerate(
        subdivisions,
        start=1,
    ):
        code = subdivision[
            "code"
        ]

        name = subdivision[
            "name"
        ]

        url = subdivision[
            "url"
        ]

        print(
            f"  [{index}/{len(subdivisions)}] "
            f"{name} ({code})"
        )

        try:
            text = load_text(
                url,
                timeout=20,
            )

            entries = parse_m3u(
                text
            )

        except Exception as exc:
            print(
                f"    FAILED: {exc}"
            )

            failed.append(
                {
                    "code": code,
                    "name": name,
                    "error": str(exc),
                }
            )

            continue

        matched = 0
        unmatched = 0

        region_candidates = []

        for entry in entries:
            url_value = normalize_url(
                entry.get(
                    "url"
                )
            )

            if not url_value:
                continue

            channel_id = find_channel(
                entry,
                channels_by_id,
                channels_by_name,
            )

            if not channel_id:
                unmatched += 1
                continue

            candidate = make_candidate(
                channel_id=channel_id,
                url=url_value,
                source=(
                    "iptv-org-region"
                ),
                title=(
                    entry.get(
                        "title"
                    )
                    or entry.get(
                        "tvg_name"
                    )
                ),
                logo=entry.get(
                    "tvg_logo"
                ),
                group=entry.get(
                    "group"
                ),
                referrer=entry.get(
                    "referrer"
                ),
                user_agent=entry.get(
                    "user_agent"
                ),
                region_codes=[
                    code
                ],
                region_names=[
                    name
                ],
            )

            region_candidates.append(
                candidate
            )

            all_candidates.append(
                candidate
            )

            matched += 1

        stats[code] = {
            "name": name,
            "entries": len(
                entries
            ),
            "matched": matched,
            "unmatched": unmatched,
        }

    return (
        all_candidates,
        stats,
        failed,
    )


# ============================================================
# DEDUPLICATION
# ============================================================

def merge_candidates(
    candidates
):
    by_url = {}

    duplicates = 0

    for candidate in candidates:
        url = normalize_url(
            candidate.get(
                "url"
            )
        )

        if not url:
            continue

        existing = by_url.get(
            url
        )

        if existing is None:
            candidate[
                "sources"
            ] = list(
                dict.fromkeys(
                    candidate.get(
                        "sources",
                        [],
                    )
                )
            )

            candidate[
                "regions"
            ] = list(
                dict.fromkeys(
                    candidate.get(
                        "regions",
                        [],
                    )
                )
            )

            candidate[
                "region_names"
            ] = list(
                dict.fromkeys(
                    candidate.get(
                        "region_names",
                        [],
                    )
                )
            )

            by_url[
                url
            ] = candidate

            continue

        duplicates += 1

        existing[
            "sources"
        ] = list(
            dict.fromkeys(
                existing.get(
                    "sources",
                    [],
                )
                + candidate.get(
                    "sources",
                    [],
                )
            )
        )

        existing[
            "regions"
        ] = list(
            dict.fromkeys(
                existing.get(
                    "regions",
                    [],
                )
                + candidate.get(
                    "regions",
                    [],
                )
            )
        )

        existing[
            "region_names"
        ] = list(
            dict.fromkeys(
                existing.get(
                    "region_names",
                    [],
                )
                + candidate.get(
                    "region_names",
                    [],
                )
            )
        )

        if (
            not existing.get(
                "logo"
            )
            and candidate.get(
                "logo"
            )
        ):
            existing[
                "logo"
            ] = candidate[
                "logo"
            ]

        if (
            not existing.get(
                "group"
            )
            and candidate.get(
                "group"
            )
        ):
            existing[
                "group"
            ] = candidate[
                "group"
            ]

        if (
            not existing.get(
                "referrer"
            )
            and candidate.get(
                "referrer"
            )
        ):
            existing[
                "referrer"
            ] = candidate[
                "referrer"
            ]

        if (
            not existing.get(
                "user_agent"
            )
            and candidate.get(
                "user_agent"
            )
        ):
            existing[
                "user_agent"
            ] = candidate[
                "user_agent"
            ]

        if (
            not existing.get(
                "title"
            )
            and candidate.get(
                "title"
            )
        ):
            existing[
                "title"
            ] = candidate[
                "title"
            ]

    return (
        list(
            by_url.values()
        ),
        duplicates,
    )


# ============================================================
# FFMPEG / FFPROBE
# ============================================================

def probe_stream(
    candidate
):
    url = candidate[
        "url"
    ]

    command = [
        "ffprobe",
        "-v",
        "error",
        "-hide_banner",

        "-probesize",
        "2M",

        "-analyzeduration",
        "3M",

        "-rw_timeout",
        str(
            FFPROBE_TIMEOUT
            * 1_000_000
        ),

        "-select_streams",
        "v:0",

        "-show_entries",
        (
            "stream="
            "width,"
            "height,"
            "codec_name,"
            "pix_fmt,"
            "color_space,"
            "color_transfer,"
            "color_primaries,"
            "profile"
        ),

        "-of",
        "json",

        url,
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=(
                FFPROBE_TIMEOUT
                + 3
            ),
        )

        if result.returncode != 0:
            return None

        data = json.loads(
            result.stdout
        )

        streams = data.get(
            "streams"
        ) or []

        if not streams:
            return None

        video = streams[0]

        width = int(
            video.get(
                "width"
            )
            or 0
        )

        height = int(
            video.get(
                "height"
            )
            or 0
        )

        if height < MIN_HEIGHT:
            return None

        codec = safe_text(
            video.get(
                "codec_name"
            )
        ).lower()

        pix_fmt = safe_text(
            video.get(
                "pix_fmt"
            )
        ).lower()

        color_space = safe_text(
            video.get(
                "color_space"
            )
        ).lower()

        color_transfer = safe_text(
            video.get(
                "color_transfer"
            )
        ).lower()

        color_primaries = safe_text(
            video.get(
                "color_primaries"
            )
        ).lower()

        profile = safe_text(
            video.get(
                "profile"
            )
        ).lower()

        hdr = False

        if color_transfer in {
            "smpte2084",
            "arib-std-b67",
        }:
            hdr = True

        if (
            "dolby" in profile
            or "dovi" in profile
        ):
            hdr = True

        return {
            "width": width,
            "height": height,
            "codec": codec,
            "pix_fmt": pix_fmt,
            "color_space": color_space,
            "color_transfer": color_transfer,
            "color_primaries": color_primaries,
            "profile": profile,
            "hdr": hdr,
        }

    except Exception:
        return None


# ============================================================
# QUALITY SCORING
# ============================================================

def resolution_score(
    height
):
    if height >= 2160:
        return 400

    if height >= 1440:
        return 300

    if height >= 1080:
        return 200

    return 0


def codec_score(
    codec
):
    codec = safe_text(
        codec
    ).lower()

    if codec == "av1":
        return 50

    if codec in {
        "hevc",
        "h265",
        "vp9",
    }:
        return 40

    if codec in {
        "h264",
        "avc1",
    }:
        return 30

    if codec == "mpeg2video":
        return 5

    return 10


def source_score(
    candidate
):
    sources = set(
        candidate.get(
            "sources",
            [],
        )
    )

    score = 0

    # Regional source is useful because it can identify
    # an actual regional feed.
    if (
        "iptv-org-region"
        in sources
    ):
        score += 4

    if (
        "iptv-russia"
        in sources
    ):
        score += 2

    if (
        "iptv-org"
        in sources
    ):
        score += 2

    return score


def stream_score(
    candidate
):
    probe = candidate[
        "probe"
    ]

    score = 0

    score += resolution_score(
        probe.get(
            "height",
            0,
        )
    )

    if probe.get(
        "hdr"
    ):
        score += 30

    score += codec_score(
        probe.get(
            "codec",
            "",
        )
    )

    score += source_score(
        candidate
    )

    if candidate[
        "url"
    ].lower().startswith(
        "https://"
    ):
        score += 5

    if candidate.get(
        "referrer"
    ):
        score += 2

    if candidate.get(
        "user_agent"
    ):
        score += 2

    return score


def sort_streams(
    streams
):
    return sorted(
        streams,
        key=lambda x: (
            stream_score(x),

            x["probe"].get(
                "height",
                0,
            ),

            x["probe"].get(
                "width",
                0,
            ),

            1
            if x["probe"].get(
                "hdr"
            )
            else 0,

            x["url"],
        ),
        reverse=True,
    )


# ============================================================
# SELECTION
# ============================================================

def group_by_channel(
    streams
):
    result = {}

    for stream in streams:
        result.setdefault(
            stream[
                "channel"
            ],
            [],
        ).append(
            stream
        )

    return result


def best_per_channel(
    streams
):
    result = []

    for items in group_by_channel(
        streams
    ).values():
        items = sort_streams(
            items
        )

        if items:
            result.append(
                items[0]
            )

    return sort_streams(
        result
    )


def backups_per_channel(
    streams
):
    result = []

    for items in group_by_channel(
        streams
    ).values():
        items = sort_streams(
            items
        )

        result.extend(
            items[1:3]
        )

    return sort_streams(
        result
    )


def top_three_per_channel(
    streams
):
    result = []

    for items in group_by_channel(
        streams
    ).values():
        items = sort_streams(
            items
        )

        result.extend(
            items[:3]
        )

    return sort_streams(
        result
    )


# ============================================================
# M3U WRITING
# ============================================================

def channel_name(
    channel
):
    return safe_text(
        channel.get(
            "name"
        )
        or channel.get(
            "id"
        )
        or "Unknown"
    )


def channel_logo(
    channel,
    candidate,
):
    return (
        safe_text(
            candidate.get(
                "logo"
            )
        )
        or safe_text(
            channel.get(
                "logo"
            )
        )
    )


def channel_group(
    channel,
    candidate,
):
    group = safe_text(
        candidate.get(
            "group"
        )
    )

    if group:
        return group

    categories = (
        channel.get(
            "categories"
        )
        or []
    )

    if categories:
        return safe_text(
            categories[0]
        )

    return "Россия"


def source_label(
    candidate
):
    sources = "+".join(
        candidate.get(
            "sources",
            [],
        )
    )

    return sources


def region_label(
    candidate
):
    names = candidate.get(
        "region_names",
        [],
    )

    if not names:
        return ""

    if len(names) == 1:
        return names[0]

    return (
        f"{len(names)} регионов"
    )


def stream_label(
    candidate
):
    probe = candidate[
        "probe"
    ]

    width = probe.get(
        "width",
        0,
    )

    height = probe.get(
        "height",
        0,
    )

    codec = safe_text(
        probe.get(
            "codec"
        )
    ).upper()

    hdr = (
        " HDR"
        if probe.get(
            "hdr"
        )
        else ""
    )

    region = region_label(
        candidate
    )

    base = (
        f"[{width}x{height} "
        f"{codec}{hdr}] "
        f"[{source_label(candidate)}]"
    )

    if region:
        base += (
            f" [{region}]"
        )

    return base


def write_m3u(
    path,
    streams,
    channels_by_id,
):
    os.makedirs(
        os.path.dirname(path),
        exist_ok=True,
    )

    with open(
        path,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as f:
        f.write(
            "#EXTM3U\n"
        )

        for candidate in streams:
            channel = (
                channels_by_id.get(
                    candidate[
                        "channel"
                    ],
                    {},
                )
            )

            name = channel_name(
                channel
            )

            label = (
                name
                + " "
                + stream_label(
                    candidate
                )
            )

            logo = channel_logo(
                channel,
                candidate,
            )

            group = channel_group(
                channel,
                candidate,
            )

            attrs = [
                'tvg-id="'
                + m3u_escape(
                    candidate[
                        "channel"
                    ]
                )
                + '"',

                'tvg-name="'
                + m3u_escape(
                    name
                )
                + '"',

                'group-title="'
                + m3u_escape(
                    group
                )
                + '"',
            ]

            if logo:
                attrs.append(
                    'tvg-logo="'
                    + m3u_escape(
                        logo
                    )
                    + '"'
                )

            if candidate.get(
                "referrer"
            ):
                attrs.append(
                    'http-referrer="'
                    + m3u_escape(
                        candidate[
                            "referrer"
                        ]
                    )
                    + '"'
                )

            if candidate.get(
                "user_agent"
            ):
                attrs.append(
                    'http-user-agent="'
                    + m3u_escape(
                        candidate[
                            "user_agent"
                        ]
                    )
                    + '"'
                )

            f.write(
                "#EXTINF:-1 "
                + " ".join(
                    attrs
                )
                + ","
                + m3u_escape(
                    label
                )
                + "\n"
            )

            # VLC compatibility.
            if candidate.get(
                "referrer"
            ):
                f.write(
                    "#EXTVLCOPT:http-referrer="
                    + candidate[
                        "referrer"
                    ]
                    + "\n"
                )

            if candidate.get(
                "user_agent"
            ):
                f.write(
                    "#EXTVLCOPT:http-user-agent="
                    + candidate[
                        "user_agent"
                    ]
                    + "\n"
                )

            f.write(
                candidate[
                    "url"
                ]
                + "\n"
            )


# ============================================================
# REGION PLAYLISTS
# ============================================================

def write_region_playlists(
    working,
    channels_by_id,
):
    os.makedirs(
        REGION_OUTPUT_DIR,
        exist_ok=True,
    )

    grouped = {}

    for stream in working:
        for region in stream.get(
            "regions",
            [],
        ):
            grouped.setdefault(
                region,
                [],
            ).append(
                stream
            )

    result = {}

    for region_code, streams in grouped.items():
        streams = sort_streams(
            streams
        )

        # One best stream per channel
        best = best_per_channel(
            streams
        )

        path = os.path.join(
            REGION_OUTPUT_DIR,
            region_code + ".m3u",
        )

        write_m3u(
            path,
            best,
            channels_by_id,
        )

        result[
            region_code
        ] = {
            "streams": len(
                streams
            ),
            "channels": len(
                {
                    x[
                        "channel"
                    ]
                    for x in streams
                }
            ),
            "best_streams": len(
                best
            ),
            "file": (
                "regions/"
                + region_code
                + ".m3u"
            ),
        }

    return result


# ============================================================
# STATISTICS
# ============================================================

def resolution_distribution(
    streams
):
    result = {}

    for stream in streams:
        height = str(
            stream[
                "probe"
            ].get(
                "height",
                0,
            )
        )

        result[
            height
        ] = (
            result.get(
                height,
                0,
            )
            + 1
        )

    return dict(
        sorted(
            result.items(),
            key=lambda x: int(
                x[0]
            ),
            reverse=True,
        )
    )


def codec_distribution(
    streams
):
    result = {}

    for stream in streams:
        codec = (
            stream[
                "probe"
            ].get(
                "codec"
            )
            or "unknown"
        )

        result[
            codec
        ] = (
            result.get(
                codec,
                0,
            )
            + 1
        )

    return dict(
        sorted(
            result.items(),
            key=lambda x: (
                -x[1],
                x[0],
            ),
        )
    )


def source_distribution(
    streams
):
    result = {}

    for stream in streams:
        for source in stream.get(
            "sources",
            [],
        ):
            result[
                source
            ] = (
                result.get(
                    source,
                    0,
                )
                + 1
            )

    return result


def region_distribution(
    streams
):
    result = {}

    for stream in streams:
        for region in stream.get(
            "region_names",
            [],
        ):
            result[
                region
            ] = (
                result.get(
                    region,
                    0,
                )
                + 1
            )

    return dict(
        sorted(
            result.items(),
            key=lambda x: (
                -x[1],
                x[0],
            ),
        )
    )


# ============================================================
# MAIN
# ============================================================

def main():

    os.makedirs(
        OUTPUT_DIR,
        exist_ok=True,
    )

    os.makedirs(
        REGION_OUTPUT_DIR,
        exist_ok=True,
    )

    print(
        "=========================================="
    )
    print(
        "Russia IPTV V7"
    )
    print(
        "Minimum actual resolution: 1080p"
    )
    print(
        "Sources: iptv-org + iptv-russia + regions"
    )
    print(
        "=========================================="
    )

    # --------------------------------------------------------
    # 1. Load API
    # --------------------------------------------------------

    print(
        "\n[1/9] Loading iptv-org API..."
    )

    channels = load_json(
        "channels.json"
    )

    streams = load_json(
        "streams.json"
    )

    subdivisions = load_json(
        "subdivisions.json"
    )

    (
        channels_by_id,
        channels_by_name,
    ) = build_channel_indexes(
        channels
    )

    print(
        "Russian channels:",
        len(
            channels_by_id
        ),
    )

    # --------------------------------------------------------
    # 2. API candidates
    # --------------------------------------------------------

    print(
        "\n[2/9] Building iptv-org API candidates..."
    )

    api_candidates = (
        build_api_candidates(
            streams,
            channels_by_id,
        )
    )

    print(
        "iptv-org API candidates:",
        len(
            api_candidates
        ),
    )

    # --------------------------------------------------------
    # 3. iptv-russia
    # --------------------------------------------------------

    print(
        "\n[3/9] Loading iptv-russia..."
    )

    russia_text = load_text(
        IPTV_RUSSIA_M3U
    )

    russia_entries = parse_m3u(
        russia_text
    )

    (
        russia_candidates,
        russia_unmatched,
    ) = build_russia_candidates(
        russia_entries,
        channels_by_id,
        channels_by_name,
    )

    print(
        "iptv-russia entries:",
        len(
            russia_entries
        ),
    )

    print(
        "iptv-russia matched:",
        len(
            russia_candidates
        ),
    )

    print(
        "iptv-russia unmatched:",
        russia_unmatched,
    )

    # --------------------------------------------------------
    # 4. Russian subdivisions
    # --------------------------------------------------------

    print(
        "\n[4/9] Loading Russian regional playlists..."
    )

    subdivision_list = (
        build_subdivision_list(
            subdivisions
        )
    )

    print(
        "Russian subdivisions:",
        len(
            subdivision_list
        ),
    )

    (
        regional_candidates,
        regional_stats,
        regional_failed,
    ) = load_subdivision_playlists(
        subdivision_list,
        channels_by_id,
        channels_by_name,
    )

    print(
        "Regional candidates:",
        len(
            regional_candidates
        ),
    )

    print(
        "Regional playlists failed:",
        len(
            regional_failed
        ),
    )

    # --------------------------------------------------------
    # 5. Merge all
    # --------------------------------------------------------

    print(
        "\n[5/9] Merging and deduplicating..."
    )

    all_candidates = (
        api_candidates
        + russia_candidates
        + regional_candidates
    )

    (
        merged_candidates,
        duplicates_removed,
    ) = merge_candidates(
        all_candidates
    )

    print(
        "Candidates before dedup:",
        len(
            all_candidates
        ),
    )

    print(
        "Unique URLs:",
        len(
            merged_candidates
        ),
    )

    print(
        "Duplicates removed:",
        duplicates_removed,
    )

    # --------------------------------------------------------
    # 6. ffprobe
    # --------------------------------------------------------

    print(
        "\n[6/9] ffprobe validation..."
    )

    working = []

    total = len(
        merged_candidates
    )

    completed = 0

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:

        future_map = {
            executor.submit(
                probe_stream,
                candidate,
            ): candidate
            for candidate
            in merged_candidates
        }

        for future in as_completed(
            future_map
        ):
            candidate = (
                future_map[
                    future
                ]
            )

            try:
                probe = future.result()

            except Exception:
                probe = None

            completed += 1

            if probe is not None:
                candidate[
                    "probe"
                ] = probe

                working.append(
                    candidate
                )

            if (
                completed % 25 == 0
                or completed == total
            ):
                print(
                    f"  checked "
                    f"{completed}/{total} "
                    f"| working 1080p+: "
                    f"{len(working)}"
                )

    print(
        "\nWorking 1080p+:",
        len(
            working
        ),
    )

    # --------------------------------------------------------
    # 7. Select
    # --------------------------------------------------------

    print(
        "\n[7/9] Selecting streams..."
    )

    working = sort_streams(
        working
    )

    best = best_per_channel(
        working
    )

    backups = backups_per_channel(
        working
    )

    selected = top_three_per_channel(
        working
    )

    fhd = [
        x
        for x in working
        if x[
            "probe"
        ][
            "height"
        ] >= 1080
    ]

    four_k = [
        x
        for x in working
        if x[
            "probe"
        ][
            "height"
        ] >= 2160
    ]

    hdr = [
        x
        for x in working
        if x[
            "probe"
        ].get(
            "hdr"
        )
    ]

    # --------------------------------------------------------
    # 8. Write
    # --------------------------------------------------------

    print(
        "\n[8/9] Writing playlists..."
    )

    write_m3u(
        os.path.join(
            OUTPUT_DIR,
            "russia.m3u",
        ),
        selected,
        channels_by_id,
    )

    write_m3u(
        os.path.join(
            OUTPUT_DIR,
            "russia-best.m3u",
        ),
        best,
        channels_by_id,
    )

    write_m3u(
        os.path.join(
            OUTPUT_DIR,
            "russia-backup.m3u",
        ),
        backups,
        channels_by_id,
    )

    write_m3u(
        os.path.join(
            OUTPUT_DIR,
            "russia-fhd.m3u",
        ),
        fhd,
        channels_by_id,
    )

    write_m3u(
        os.path.join(
            OUTPUT_DIR,
            "russia-4k.m3u",
        ),
        four_k,
        channels_by_id,
    )

    write_m3u(
        os.path.join(
            OUTPUT_DIR,
            "russia-hdr.m3u",
        ),
        hdr,
        channels_by_id,
    )

    region_report = (
        write_region_playlists(
            working,
            channels_by_id,
        )
    )

    # --------------------------------------------------------
    # 9. Report
    # --------------------------------------------------------

    print(
        "\n[9/9] Writing report..."
    )

    report = {
        "version": 7,

        "filter": {
            "minimum_height": MIN_HEIGHT,
            "minimum_quality": "1080p",
            "actual_ffprobe_validation": True,
        },

        "sources": {
            "iptv-org": {
                "candidates": len(
                    api_candidates
                ),
            },

            "iptv-russia": {
                "m3u_entries": len(
                    russia_entries
                ),
                "matched_candidates": len(
                    russia_candidates
                ),
                "unmatched_entries": (
                    russia_unmatched
                ),
            },

            "iptv-org-regions": {
                "subdivisions": len(
                    subdivision_list
                ),
                "candidates": len(
                    regional_candidates
                ),
                "failed_playlists": len(
                    regional_failed
                ),
            },
        },

        "russian_channels": len(
            channels_by_id
        ),

        "candidate_streams_before_dedup": len(
            all_candidates
        ),

        "unique_candidate_streams": len(
            merged_candidates
        ),

        "duplicates_removed": (
            duplicates_removed
        ),

        "working_1080p_plus": len(
            working
        ),

        "channels_with_1080p_plus": len(
            {
                x[
                    "channel"
                ]
                for x in working
            }
        ),

        "selected_streams": len(
            selected
        ),

        "best_streams": len(
            best
        ),

        "backup_streams": len(
            backups
        ),

        "fhd_streams": len(
            fhd
        ),

        "4k_streams": len(
            four_k
        ),

        "hdr_streams": len(
            hdr
        ),

        "resolution_distribution": (
            resolution_distribution(
                working
            )
        ),

        "video_codecs": (
            codec_distribution(
                working
            )
        ),

        "working_streams_by_source": (
            source_distribution(
                working
            )
        ),

        "regional_working_streams": (
            region_distribution(
                working
            )
        ),

        "regional_playlists": (
            region_report
        ),

        "regional_playlist_errors": (
            regional_failed
        ),

        "regional_playlist_stats": (
            regional_stats
        ),
    }

    with open(
        os.path.join(
            OUTPUT_DIR,
            "report.json",
        ),
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            report,
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(
        "\n=========================================="
    )
    print(
        "V7 COMPLETE"
    )
    print(
        "=========================================="
    )

    print(
        "Russian channels:",
        report[
            "russian_channels"
        ],
    )

    print(
        "Candidates:",
        report[
            "candidate_streams_before_dedup"
        ],
    )

    print(
        "Unique URLs:",
        report[
            "unique_candidate_streams"
        ],
    )

    print(
        "Duplicates:",
        report[
            "duplicates_removed"
        ],
    )

    print(
        "Working 1080p+:",
        report[
            "working_1080p_plus"
        ],
    )

    print(
        "Channels 1080p+:",
        report[
            "channels_with_1080p_plus"
        ],
    )

    print(
        "Best:",
        report[
            "best_streams"
        ],
    )

    print(
        "Backup:",
        report[
            "backup_streams"
        ],
    )

    print(
        "4K:",
        report[
            "4k_streams"
        ],
    )

    print(
        "HDR:",
        report[
            "hdr_streams"
        ],
    )

    print(
        "By source:",
        report[
            "working_streams_by_source"
        ],
    )

    print(
        "Resolutions:",
        report[
            "resolution_distribution"
        ],
    )

    print(
        "Codecs:",
        report[
            "video_codecs"
        ],
    )

    print(
        "Regional playlists:",
        len(
            region_report
        ),
    )

    print(
        "=========================================="
    )


if __name__ == "__main__":
    main()
