import json
import os
import re
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit, urlunsplit


# ============================================================
# V6 — Russia IPTV 1080p+
#
# Sources:
#   1. iptv-org API
#   2. substanc1/iptv-russia
#
# Final rule:
#   ONLY streams that ffprobe confirms as >= 1080p.
# ============================================================

API_BASE = "https://iptv-org.github.io/api"
IPTV_RUSSIA_M3U = (
    "https://raw.githubusercontent.com/"
    "substanc1/iptv-russia/main/streams/ru.m3u"
)

MIN_HEIGHT = 1080
WORKERS = 24
TIMEOUT_SECONDS = 8

OUTPUT_DIR = "output"


# ============================================================
# HTTP
# ============================================================

def http_get(url, timeout=30):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Russia-IPTV-V6/1.0",
            "Accept": "*/*",
        },
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def load_json(filename):
    url = f"{API_BASE}/{filename}"
    return json.loads(http_get(url).decode("utf-8"))


def load_text(url):
    return http_get(url).decode("utf-8", errors="replace")


# ============================================================
# Helpers
# ============================================================

def safe_text(value):
    if value is None:
        return ""

    return str(value).strip()


def m3u_escape(value):
    value = safe_text(value)

    if not value:
        return ""

    return value.replace('"', "'")


def normalize_url(url):
    """
    Do NOT reorder query parameters.
    Signed IPTV URLs can depend on exact query ordering.

    Only:
      - trim whitespace
      - remove URL fragment
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
        return url.split("#", 1)[0].strip()


def normalize_name(value):
    """
    Conservative channel-name normalization.

    Used only for matching external M3U entries to
    known iptv-org Russian channel IDs.
    """

    value = safe_text(value).lower()

    value = value.replace("ё", "е")

    # Remove common quality labels.
    value = re.sub(
        r"\b(?:uhd|4k|fhd|fullhd|hd|sd)\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )

    # Remove time-shift markers.
    value = re.sub(r"\s*\+\s*\d+\s*$", "", value)

    # Remove bracketed technical markers.
    value = re.sub(r"\[[^\]]*\]", " ", value)
    value = re.sub(r"\([^)]*\)", " ", value)

    # Punctuation -> spaces.
    value = re.sub(r"[^a-zа-я0-9]+", " ", value)

    # Collapse whitespace.
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def detect_quality_label(text):
    """
    Read declared resolution from an M3U title.

    This is informational only.
    Final filtering is done by ffprobe.
    """

    text = safe_text(text).lower()

    match = re.search(r"(\d{3,4})\s*[pi]", text)

    if match:
        return f"{match.group(1)}p"

    if "4k" in text or "uhd" in text:
        return "2160p"

    if "fhd" in text or "fullhd" in text:
        return "1080p"

    if re.search(r"\bhd\b", text):
        return "hd"

    if re.search(r"\bsd\b", text):
        return "sd"

    return ""


# ============================================================
# M3U parser
# ============================================================

EXTINF_RE = re.compile(
    r'^#EXTINF:(?P<duration>-?\d+)\s*(?P<attrs>.*?)'
    r',(?P<title>.*)$'
)


def parse_m3u_attributes(text):
    attrs = {}

    pattern = re.compile(
        r'([A-Za-z0-9_-]+)="([^"]*)"'
    )

    for key, value in pattern.findall(text):
        attrs[key.lower()] = value

    return attrs


def parse_external_m3u(text):
    """
    Parse substanc1/iptv-russia M3U.

    Supports:
      #EXTINF
      #EXTVLCOPT:http-referrer
      #EXTVLCOPT:http-user-agent
    """

    entries = []

    current = None
    pending_referrer = ""
    pending_user_agent = ""

    lines = text.splitlines()

    for raw_line in lines:
        line = raw_line.strip()

        if not line:
            continue

        if line.startswith("#EXTINF:"):
            match = EXTINF_RE.match(line)

            if not match:
                current = None
                continue

            attrs = parse_m3u_attributes(match.group("attrs"))

            title = safe_text(match.group("title"))

            current = {
                "tvg_id": safe_text(attrs.get("tvg-id")),
                "tvg_name": safe_text(
                    attrs.get("tvg-name") or title
                ),
                "tvg_logo": safe_text(attrs.get("tvg-logo")),
                "group": safe_text(attrs.get("group-title")),
                "title": title,
                "referrer": "",
                "user_agent": "",
            }

            pending_referrer = ""
            pending_user_agent = ""

            continue

        if line.startswith("#EXTVLCOPT:http-referrer="):
            pending_referrer = line.split(
                "=", 1
            )[1].strip()

            continue

        if line.startswith("#EXTVLCOPT:http-user-agent="):
            pending_user_agent = line.split(
                "=", 1
            )[1].strip()

            continue

        if line.startswith("#"):
            continue

        # URL line
        if current and (
            line.startswith("http://")
            or line.startswith("https://")
        ):
            current["url"] = line
            current["referrer"] = pending_referrer
            current["user_agent"] = pending_user_agent

            entries.append(current)

            current = None
            pending_referrer = ""
            pending_user_agent = ""

    return entries


# ============================================================
# iptv-org channel database
# ============================================================

def build_channel_indexes(channels):
    by_id = {}
    by_name = {}

    for channel in channels:
        channel_id = safe_text(channel.get("id"))

        if not channel_id:
            continue

        country = safe_text(channel.get("country"))

        if country.lower() != "ru":
            continue

        by_id[channel_id] = channel

        names = []

        main_name = safe_text(channel.get("name"))

        if main_name:
            names.append(main_name)

        alt_names = channel.get("alt_names") or []

        if isinstance(alt_names, list):
            names.extend(
                safe_text(x)
                for x in alt_names
                if safe_text(x)
            )

        for name in names:
            normalized = normalize_name(name)

            if normalized and normalized not in by_name:
                by_name[normalized] = channel_id

    return by_id, by_name


def find_channel_for_external(entry, by_id, by_name):
    """
    First choice:
      exact tvg-id

    Second:
      exact normalized channel name

    We deliberately do NOT use aggressive fuzzy matching.
    It can incorrectly merge different Russian channels.
    """

    tvg_id = safe_text(entry.get("tvg_id"))

    if tvg_id in by_id:
        return tvg_id

    title_candidates = [
        entry.get("tvg_name"),
        entry.get("title"),
    ]

    for value in title_candidates:
        normalized = normalize_name(value)

        if not normalized:
            continue

        channel_id = by_name.get(normalized)

        if channel_id:
            return channel_id

    return ""


# ============================================================
# iptv-org stream candidates
# ============================================================

def build_iptv_org_candidates(streams, channels_by_id):
    candidates = []

    for stream in streams:
        channel_id = safe_text(stream.get("channel"))

        if not channel_id:
            continue

        if channel_id not in channels_by_id:
            continue

        url = normalize_url(stream.get("url"))

        if not url:
            continue

        candidates.append(
            {
                "channel": channel_id,
                "url": url,
                "source": "iptv-org",
                "sources": ["iptv-org"],
                "title": safe_text(
                    stream.get("label")
                    or stream.get("quality")
                    or ""
                ),
                "logo": "",
                "group": "",
                "referrer": safe_text(
                    stream.get("referrer")
                ),
                "user_agent": safe_text(
                    stream.get("user_agent")
                ),
                "declared_quality": safe_text(
                    stream.get("quality")
                ),
                "feed": safe_text(
                    stream.get("feed")
                ),
            }
        )

    return candidates


# ============================================================
# External source candidates
# ============================================================

def build_external_candidates(
    external_entries,
    channels_by_id,
    channels_by_name,
):
    candidates = []
    unmatched = 0

    for entry in external_entries:
        url = normalize_url(entry.get("url"))

        if not url:
            continue

        channel_id = find_channel_for_external(
            entry,
            channels_by_id,
            channels_by_name,
        )

        if not channel_id:
            unmatched += 1
            continue

        title = safe_text(
            entry.get("title")
            or entry.get("tvg_name")
        )

        candidates.append(
            {
                "channel": channel_id,
                "url": url,
                "source": "iptv-russia",
                "sources": ["iptv-russia"],
                "title": title,
                "logo": safe_text(
                    entry.get("tvg_logo")
                ),
                "group": safe_text(
                    entry.get("group")
                ),
                "referrer": safe_text(
                    entry.get("referrer")
                ),
                "user_agent": safe_text(
                    entry.get("user_agent")
                ),
                "declared_quality": detect_quality_label(
                    title
                ),
                "feed": "",
            }
        )

    return candidates, unmatched


# ============================================================
# Deduplication
# ============================================================

def merge_duplicate_streams(candidates):
    """
    Same URL can appear in both sources.

    Keep one stream but remember all sources.
    """

    result = {}
    duplicates_removed = 0

    for candidate in candidates:
        key = normalize_url(candidate["url"])

        if not key:
            continue

        existing = result.get(key)

        if existing is None:
            candidate["sources"] = list(
                dict.fromkeys(
                    candidate.get("sources", [])
                )
            )

            result[key] = candidate
            continue

        duplicates_removed += 1

        old_sources = existing.get(
            "sources", []
        )

        new_sources = candidate.get(
            "sources", []
        )

        existing["sources"] = list(
            dict.fromkeys(
                old_sources + new_sources
            )
        )

        # Prefer metadata from the entry containing
        # more useful information.
        if not existing.get("logo") and candidate.get("logo"):
            existing["logo"] = candidate["logo"]

        if not existing.get("group") and candidate.get("group"):
            existing["group"] = candidate["group"]

        if not existing.get("referrer") and candidate.get("referrer"):
            existing["referrer"] = candidate["referrer"]

        if not existing.get("user_agent") and candidate.get("user_agent"):
            existing["user_agent"] = candidate["user_agent"]

        if not existing.get("title") and candidate.get("title"):
            existing["title"] = candidate["title"]

    return list(result.values()), duplicates_removed


# ============================================================
# ffprobe
# ============================================================

def probe_stream(candidate):
    url = candidate["url"]

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
        str(TIMEOUT_SECONDS * 1_000_000),
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream="
        "width,"
        "height,"
        "codec_name,"
        "pix_fmt,"
        "color_space,"
        "color_transfer,"
        "color_primaries,"
        "profile",
        "-of",
        "json",
        url,
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS + 3,
        )

        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)

        streams = data.get("streams") or []

        if not streams:
            return None

        video = streams[0]

        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)

        if height < MIN_HEIGHT:
            return None

        codec = safe_text(
            video.get("codec_name")
        ).lower()

        pix_fmt = safe_text(
            video.get("pix_fmt")
        ).lower()

        color_space = safe_text(
            video.get("color_space")
        ).lower()

        color_transfer = safe_text(
            video.get("color_transfer")
        ).lower()

        color_primaries = safe_text(
            video.get("color_primaries")
        ).lower()

        profile = safe_text(
            video.get("profile")
        ).lower()

        hdr = False

        # HDR is confirmed only by actual HDR transfer /
        # Dolby Vision indicators.
        if color_transfer in {
            "smpte2084",
            "arib-std-b67",
        }:
            hdr = True

        if "dolby" in profile or "dovi" in profile:
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
# Quality ranking
# ============================================================

def resolution_score(height):
    if height >= 2160:
        return 400

    if height >= 1440:
        return 300

    if height >= 1080:
        return 200

    return 0


def codec_score(codec):
    codec = codec.lower()

    if codec in {
        "av1",
    }:
        return 40

    if codec in {
        "hevc",
        "h265",
        "vp9",
    }:
        return 30

    if codec in {
        "h264",
        "avc1",
    }:
        return 20

    if codec in {
        "mpeg2video",
    }:
        return 5

    return 10


def audio_hint_score(candidate):
    """
    ffprobe here checks video first for speed.

    Audio score is therefore only a small metadata hint.
    """

    text = (
        safe_text(candidate.get("title"))
        + " "
        + safe_text(candidate.get("declared_quality"))
    ).lower()

    if "eac3" in text or "dd+" in text:
        return 8

    if "ac3" in text or "dolby" in text:
        return 6

    if "aac" in text:
        return 4

    return 0


def stream_score(candidate):
    probe = candidate["probe"]

    score = 0

    score += resolution_score(
        probe["height"]
    )

    if probe.get("hdr"):
        score += 25

    score += codec_score(
        probe.get("codec", "")
    )

    score += audio_hint_score(
        candidate
    )

    if candidate["url"].lower().startswith(
        "https://"
    ):
        score += 5

    # Small bonus if the stream has playback headers.
    if candidate.get("referrer"):
        score += 2

    if candidate.get("user_agent"):
        score += 2

    return score


# ============================================================
# M3U writer
# ============================================================

def channel_name(channel):
    return safe_text(
        channel.get("name")
        or channel.get("id")
        or "Unknown"
    )


def channel_logo(channel, candidate):
    return (
        safe_text(candidate.get("logo"))
        or safe_text(channel.get("logo"))
    )


def channel_group(channel, candidate):
    candidate_group = safe_text(
        candidate.get("group")
    )

    if candidate_group:
        return candidate_group

    categories = channel.get("categories") or []

    if categories:
        return safe_text(categories[0])

    return "Россия"


def stream_label(candidate):
    probe = candidate["probe"]

    resolution = (
        f'{probe["width"]}x{probe["height"]}'
        if probe.get("width")
        else f'{probe["height"]}p'
    )

    codec = safe_text(
        probe.get("codec")
    ).upper()

    hdr = " HDR" if probe.get("hdr") else ""

    source_names = "+".join(
        candidate.get("sources", [])
    )

    return (
        f'[{resolution} {codec}{hdr}]'
        f' [{source_names}]'
    )


def write_m3u(filename, streams, channels_by_id):
    path = os.path.join(
        OUTPUT_DIR,
        filename,
    )

    with open(
        path,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as f:
        f.write("#EXTM3U\n")

        for candidate in streams:
            channel = channels_by_id.get(
                candidate["channel"],
                {},
            )

            name = channel_name(channel)

            label = (
                f"{name} "
                f"{stream_label(candidate)}"
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
                    candidate["channel"]
                )
                + '"',

                'tvg-name="'
                + m3u_escape(name)
                + '"',

                'group-title="'
                + m3u_escape(group)
                + '"',
            ]

            if logo:
                attrs.append(
                    'tvg-logo="'
                    + m3u_escape(logo)
                    + '"'
                )

            if candidate.get("referrer"):
                attrs.append(
                    'http-referrer="'
                    + m3u_escape(
                        candidate["referrer"]
                    )
                    + '"'
                )

            if candidate.get("user_agent"):
                attrs.append(
                    'http-user-agent="'
                    + m3u_escape(
                        candidate["user_agent"]
                    )
                    + '"'
                )

            f.write(
                "#EXTINF:-1 "
                + " ".join(attrs)
                + ","
                + m3u_escape(label)
                + "\n"
            )

            f.write(
                candidate["url"]
                + "\n"
            )

    return path


# ============================================================
# Selection
# ============================================================

def sort_streams(streams):
    return sorted(
        streams,
        key=lambda x: (
            stream_score(x),
            x["probe"].get("height", 0),
            x["probe"].get("width", 0),
            x["probe"].get("hdr", False),
            x["url"],
        ),
        reverse=True,
    )


def best_per_channel(streams):
    grouped = {}

    for stream in streams:
        grouped.setdefault(
            stream["channel"],
            [],
        ).append(stream)

    result = []

    for channel_id, items in grouped.items():
        items = sort_streams(items)

        if items:
            result.append(items[0])

    return sort_streams(result)


def backups_per_channel(streams):
    grouped = {}

    for stream in streams:
        grouped.setdefault(
            stream["channel"],
            [],
        ).append(stream)

    result = []

    for channel_id, items in grouped.items():
        items = sort_streams(items)

        # 2 backups after the best.
        result.extend(items[1:3])

    return sort_streams(result)


def top_three_per_channel(streams):
    grouped = {}

    for stream in streams:
        grouped.setdefault(
            stream["channel"],
            [],
        ).append(stream)

    result = []

    for channel_id, items in grouped.items():
        items = sort_streams(items)

        result.extend(items[:3])

    return sort_streams(result)


# ============================================================
# Statistics
# ============================================================

def resolution_distribution(streams):
    result = {}

    for stream in streams:
        height = str(
            stream["probe"].get(
                "height",
                0,
            )
        )

        result[height] = (
            result.get(height, 0) + 1
        )

    return dict(
        sorted(
            result.items(),
            key=lambda x: int(x[0]),
            reverse=True,
        )
    )


def codec_distribution(streams):
    result = {}

    for stream in streams:
        codec = (
            stream["probe"].get("codec")
            or "unknown"
        )

        result[codec] = (
            result.get(codec, 0) + 1
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


def source_distribution(streams):
    result = {}

    for stream in streams:
        sources = stream.get(
            "sources",
            [],
        )

        for source in sources:
            result[source] = (
                result.get(source, 0) + 1
            )

    return result


# ============================================================
# Main
# ============================================================

def main():
    os.makedirs(
        OUTPUT_DIR,
        exist_ok=True,
    )

    print("======================================")
    print("Russia IPTV Builder V6")
    print("Minimum quality: 1080p+")
    print("======================================")

    # --------------------------------------------------------
    # Load iptv-org
    # --------------------------------------------------------

    print("\n[1/8] Loading iptv-org API...")

    channels = load_json(
        "channels.json"
    )

    streams = load_json(
        "streams.json"
    )

    channels_by_id, channels_by_name = (
        build_channel_indexes(channels)
    )

    print(
        f"Russian channels: "
        f"{len(channels_by_id)}"
    )

    # --------------------------------------------------------
    # Source 1
    # --------------------------------------------------------

    print("\n[2/8] Building iptv-org candidates...")

    iptv_org_candidates = (
        build_iptv_org_candidates(
            streams,
            channels_by_id,
        )
    )

    print(
        f"iptv-org candidates: "
        f"{len(iptv_org_candidates)}"
    )

    # --------------------------------------------------------
    # Source 2
    # --------------------------------------------------------

    print(
        "\n[3/8] Downloading iptv-russia..."
    )

    external_text = load_text(
        IPTV_RUSSIA_M3U
    )

    external_entries = parse_external_m3u(
        external_text
    )

    print(
        f"iptv-russia M3U entries: "
        f"{len(external_entries)}"
    )

    iptv_russia_candidates, unmatched = (
        build_external_candidates(
            external_entries,
            channels_by_id,
            channels_by_name,
        )
    )

    print(
        f"iptv-russia matched candidates: "
        f"{len(iptv_russia_candidates)}"
    )

    print(
        f"iptv-russia unmatched entries: "
        f"{unmatched}"
    )

    # --------------------------------------------------------
    # Merge
    # --------------------------------------------------------

    print("\n[4/8] Merging sources...")

    all_candidates = (
        iptv_org_candidates
        + iptv_russia_candidates
    )

    merged_candidates, duplicates_removed = (
        merge_duplicate_streams(
            all_candidates
        )
    )

    print(
        f"Total candidates: "
        f"{len(all_candidates)}"
    )

    print(
        f"Unique URLs: "
        f"{len(merged_candidates)}"
    )

    print(
        f"Duplicates removed: "
        f"{duplicates_removed}"
    )

    # --------------------------------------------------------
    # ffprobe
    # --------------------------------------------------------

    print(
        "\n[5/8] Checking actual video resolution..."
    )

    working = []

    completed = 0
    total = len(merged_candidates)

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:

        future_map = {
            executor.submit(
                probe_stream,
                candidate,
            ): candidate
            for candidate in merged_candidates
        }

        for future in as_completed(
            future_map
        ):
            candidate = future_map[future]

            try:
                probe = future.result()
            except Exception:
                probe = None

            completed += 1

            if probe is None:
                continue

            candidate["probe"] = probe

            working.append(candidate)

            if completed % 25 == 0 or completed == total:
                print(
                    f"  checked "
                    f"{completed}/{total} "
                    f"| 1080p+ working: "
                    f"{len(working)}"
                )

    print(
        f"\nWorking 1080p+: "
        f"{len(working)}"
    )

    # --------------------------------------------------------
    # Sort / select
    # --------------------------------------------------------

    print(
        "\n[6/8] Selecting best streams..."
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

    top_three = top_three_per_channel(
        working
    )

    fhd = [
        x
        for x in working
        if x["probe"]["height"] >= 1080
    ]

    four_k = [
        x
        for x in working
        if x["probe"]["height"] >= 2160
    ]

    hdr = [
        x
        for x in working
        if x["probe"].get("hdr")
    ]

    # --------------------------------------------------------
    # Write playlists
    # --------------------------------------------------------

    print(
        "\n[7/8] Writing playlists..."
    )

    write_m3u(
        "russia.m3u",
        top_three,
        channels_by_id,
    )

    write_m3u(
        "russia-best.m3u",
        best,
        channels_by_id,
    )

    write_m3u(
        "russia-fhd.m3u",
        fhd,
        channels_by_id,
    )

    write_m3u(
        "russia-4k.m3u",
        four_k,
        channels_by_id,
    )

    write_m3u(
        "russia-hdr.m3u",
        hdr,
        channels_by_id,
    )

    write_m3u(
        "russia-backup.m3u",
        backups,
        channels_by_id,
    )

    # --------------------------------------------------------
    # Report
    # --------------------------------------------------------

    report = {
        "version": 6,

        "filter": {
            "minimum_height": MIN_HEIGHT,
            "minimum_quality": "1080p",
            "actual_ffprobe_validation": True,
        },

        "sources": {
            "iptv-org": {
                "candidates": len(
                    iptv_org_candidates
                ),
            },
            "iptv-russia": {
                "m3u_entries": len(
                    external_entries
                ),
                "matched_candidates": len(
                    iptv_russia_candidates
                ),
                "unmatched_entries": unmatched,
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
                x["channel"]
                for x in working
            }
        ),

        "selected_streams": len(
            top_three
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

    # --------------------------------------------------------
    # Console report
    # --------------------------------------------------------

    print("\n[8/8] DONE")
    print("--------------------------------------")
    print(
        "Russian channels:",
        report["russian_channels"],
    )
    print(
        "Candidates before dedup:",
        report[
            "candidate_streams_before_dedup"
        ],
    )
    print(
        "Unique candidates:",
        report[
            "unique_candidate_streams"
        ],
    )
    print(
        "Duplicates removed:",
        report["duplicates_removed"],
    )
    print(
        "Working 1080p+:",
        report["working_1080p_plus"],
    )
    print(
        "Channels with 1080p+:",
        report[
            "channels_with_1080p_plus"
        ],
    )
    print(
        "Best:",
        report["best_streams"],
    )
    print(
        "Backups:",
        report["backup_streams"],
    )
    print(
        "4K:",
        report["4k_streams"],
    )
    print(
        "HDR:",
        report["hdr_streams"],
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
        report["video_codecs"],
    )
    print("--------------------------------------")


if __name__ == "__main__":
    main()
