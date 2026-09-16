import json
import os
import re
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


# ============================================================
# RUSSIA IPTV AGGREGATOR V4
# ============================================================

API_BASE = "https://iptv-org.github.io/api"
OUTPUT_DIR = "output"

# Количество одновременных ffprobe
WORKERS = 24

# Сколько секунд даём одному потоку
TIMEOUT_SECONDS = 8

# Максимум потоков одного канала в основном списке
MAX_STREAMS_PER_CHANNEL = 3

# Сколько потоков оставляем в BEST
MAX_BEST_PER_CHANNEL = 1

# Сколько потоков оставляем в BACKUP
MAX_BACKUP_PER_CHANNEL = 2

USER_AGENT = (
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "Chrome/128 Safari/537.36"
)

os.makedirs(OUTPUT_DIR, exist_ok=True)


# ============================================================
# GLOBAL DATA
# ============================================================

channels_by_id = {}
feeds_by_id = {}
logos_by_channel = {}
subdivisions_by_code = {}


# ============================================================
# DOWNLOAD JSON
# ============================================================

def load_json(name):
    url = f"{API_BASE}/{name}.json"

    print()
    print("=" * 60)
    print("Downloading:", url)

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()

        result = json.loads(data)

        print("Loaded:", name, "items:", len(result))

        return result

    except Exception as error:
        print("ERROR loading", name, ":", error)
        raise


# ============================================================
# NORMALIZATION
# ============================================================

def normalize_url(url):
    if not url:
        return ""

    return str(url).strip()


def normalize_text(value):
    if not value:
        return ""

    return str(value).strip().lower()


# ============================================================
# HDR DETECTION
# ============================================================

def detect_hdr(video_stream):
    values = []

    for key in (
        "color_transfer",
        "color_space",
        "color_primaries",
        "pix_fmt",
    ):
        value = video_stream.get(key)

        if value:
            values.append(str(value).lower())

    side_data = video_stream.get("side_data_list")

    if side_data:
        values.append(str(side_data).lower())

    text = " ".join(values)

    # PQ / HDR10
    if "smpte2084" in text:
        return True

    # HLG
    if "arib-std-b67" in text:
        return True

    # Dolby Vision metadata
    if "dolby vision" in text:
        return True

    if "dovi" in text:
        return True

    # Do NOT consider BT.2020 alone as HDR.
    return False


# ============================================================
# RESOLUTION
# ============================================================

def get_resolution(width, height):
    try:
        width = int(width or 0)
        height = int(height or 0)
    except Exception:
        return 0

    if width <= 0 or height <= 0:
        return 0

    if height >= 2160:
        return 2160

    if height >= 1440:
        return 1440

    if height >= 1080:
        return 1080

    if height >= 720:
        return 720

    if height >= 576:
        return 576

    if height >= 480:
        return 480

    return height


# ============================================================
# QUALITY FROM SOURCE DATA
# ============================================================

def source_quality_score(stream):
    quality = normalize_text(stream.get("quality"))

    if "2160" in quality or "4k" in quality:
        return 400

    if "1440" in quality:
        return 300

    if "1080" in quality:
        return 200

    if "720" in quality:
        return 100

    if "576" in quality:
        return 50

    return 0


# ============================================================
# REGION
# ============================================================

def get_region_info(channel_id, feed_id):
    feed = feeds_by_id.get(feed_id)

    if not feed:
        return {
            "region_code": "",
            "region_name": "",
        }

    areas = feed.get("broadcast_area") or []

    for area in areas:
        area = str(area)

        if area.startswith("s/"):
            code = area[2:]

            subdivision = subdivisions_by_code.get(code)

            if subdivision:
                return {
                    "region_code": code,
                    "region_name": subdivision.get("name", code),
                }

            return {
                "region_code": code,
                "region_name": code,
            }

    return {
        "region_code": "",
        "region_name": "",
    }


# ============================================================
# FFPROBE
# ============================================================

def run_ffprobe(url, referrer=None, user_agent=None):
    command = [
        "ffprobe",

        "-v",
        "error",

        "-print_format",
        "json",

        "-show_streams",

        "-show_format",

        # Ограничиваем объём анализа
        "-probesize",
        "2M",

        "-analyzeduration",
        "3M",

        "-rw_timeout",
        str(TIMEOUT_SECONDS * 1_000_000),
    ]

    headers = []

    headers.append(
        "User-Agent: " + (
            user_agent
            if user_agent
            else USER_AGENT
        )
    )

    if referrer:
        headers.append(
            "Referer: " + str(referrer)
        )

    command.extend(
        [
            "-headers",
            "\r\n".join(headers) + "\r\n",
        ]
    )

    command.extend(
        [
            "-i",
            url,
        ]
    )

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=TIMEOUT_SECONDS + 3,
        )

        if result.returncode != 0:
            return None

        if not result.stdout:
            return None

        return json.loads(result.stdout)

    except subprocess.TimeoutExpired:
        return None

    except json.JSONDecodeError:
        return None

    except Exception:
        return None


# ============================================================
# CHECK ONE STREAM
# ============================================================

def check_stream(stream):
    url = normalize_url(stream.get("url"))

    if not url:
        return None

    data = run_ffprobe(
        url,
        stream.get("referrer"),
        stream.get("user_agent"),
    )

    if not data:
        return None

    video = None
    audio = None

    for item in data.get("streams", []):
        codec_type = item.get("codec_type")

        if codec_type == "video" and video is None:
            video = item

        elif codec_type == "audio" and audio is None:
            audio = item

    if not video:
        return None

    width = video.get("width", 0)
    height = video.get("height", 0)

    if not width or not height:
        return None

    resolution = get_resolution(
        width,
        height,
    )

    if resolution <= 0:
        return None

    video_codec = (
        video.get("codec_name")
        or ""
    )

    audio_codec = (
        audio.get("codec_name")
        if audio
        else ""
    )

    hdr = detect_hdr(video)

    channel_id = stream.get("channel")
    feed_id = stream.get("feed")

    region = get_region_info(
        channel_id,
        feed_id,
    )

    result = dict(stream)

    result["_online"] = True

    result["_width"] = width
    result["_height"] = height

    result["_resolution"] = resolution

    result["_video_codec"] = video_codec
    result["_audio_codec"] = audio_codec

    result["_hdr"] = hdr

    result["_region_code"] = region["region_code"]
    result["_region_name"] = region["region_name"]

    return result


# ============================================================
# STREAM SCORE
# ============================================================

def stream_score(stream):
    score = 0

    resolution = stream.get(
        "_resolution",
        0,
    )

    score += resolution * 1000

    if stream.get("_hdr"):
        score += 500

    codec = normalize_text(
        stream.get("_video_codec")
    )

    if codec in (
        "av1",
    ):
        score += 80

    elif codec in (
        "hevc",
        "h265",
    ):
        score += 60

    elif codec in (
        "h264",
        "avc1",
    ):
        score += 30

    audio = normalize_text(
        stream.get("_audio_codec")
    )

    if audio in (
        "aac",
        "ac3",
        "eac3",
    ):
        score += 10

    # Предпочитаем источники без warning label
    label = normalize_text(
        stream.get("label")
    )

    if not label:
        score += 30

    return score


# ============================================================
# CHANNEL NAME
# ============================================================

def get_channel_name(stream):
    channel_id = stream.get("channel")

    channel = channels_by_id.get(
        channel_id,
        {},
    )

    name = (
        channel.get("name")
        or stream.get("title")
        or channel_id
        or "Unknown"
    )

    return str(name)


# ============================================================
# M3U WRITER
# ============================================================

def make_playlist(streams, filename, playlist_name):
    path = os.path.join(
        OUTPUT_DIR,
        filename,
    )

    with open(
        path,
        "w",
        encoding="utf-8",
    ) as file:

        file.write(
            "#EXTM3U "
            'x-tvg-url="" '
            'url-tvg=""\n'
        )

        for stream in streams:

            channel_id = stream.get(
                "channel"
            )

            if not channel_id:
                continue

            channel = channels_by_id.get(
                channel_id
            )

            if not channel:
                continue

            name = get_channel_name(
                stream
            )

            logo = logos_by_channel.get(
                channel_id,
                "",
            )

            width = stream.get(
                "_width",
                0,
            )

            height = stream.get(
                "_height",
                0,
            )

            resolution = stream.get(
                "_resolution",
                0,
            )

            codec = stream.get(
                "_video_codec",
                "",
            )

            hdr = stream.get(
                "_hdr",
                False,
            )

            region_name = stream.get(
                "_region_name",
                "",
            )

            group = "Russia"

            categories = channel.get(
                "categories"
            ) or []

            if categories:
                group = (
                    "Russia / "
                    + str(categories[0])
                )

            attributes = [
                f'tvg-id="{channel_id}"',
                f'tvg-name="{name}"',
                f'group-title="{group}"',
            ]

            if logo:
                attributes.append(
                    f'tvg-logo="{logo}"'
                )

            label_parts = [
                name,
                f"{width}x{height}",
            ]

            if codec:
                label_parts.append(
                    codec.upper()
                )

            if hdr:
                label_parts.append(
                    "HDR"
                )

            if region_name:
                label_parts.append(
                    region_name
                )

            label = " | ".join(
                label_parts
            )

            file.write(
                "#EXTINF:-1 "
                + " ".join(attributes)
                + ","
                + label
                + "\n"
            )

            file.write(
                stream["url"]
                + "\n"
            )

    print(
        "Created:",
        filename,
        "streams:",
        len(streams),
    )


# ============================================================
# MAIN
# ============================================================

print()
print("=" * 70)
print("RUSSIA IPTV AGGREGATOR V4")
print("=" * 70)
print()

# ------------------------------------------------------------
# Load API
# ------------------------------------------------------------

channels = load_json(
    "channels"
)

streams = load_json(
    "streams"
)

logos = load_json(
    "logos"
)

feeds = load_json(
    "feeds"
)

subdivisions = load_json(
    "subdivisions"
)


# ------------------------------------------------------------
# Russian channels
# ------------------------------------------------------------

channels_by_id = {
    channel["id"]: channel
    for channel in channels
    if channel.get("country") == "RU"
}

print()
print(
    "Russian channels:",
    len(channels_by_id),
)


# ------------------------------------------------------------
# Feeds
# ------------------------------------------------------------

feeds_by_id = {
    feed["id"]: feed
    for feed in feeds
    if feed.get("id")
}


print(
    "Feeds:",
    len(feeds_by_id),
)


# ------------------------------------------------------------
# Subdivisions
# ------------------------------------------------------------

subdivisions_by_code = {
    item["code"]: item
    for item in subdivisions
    if item.get("code")
}


print(
    "Russian subdivisions database:",
    len(subdivisions_by_code),
)


# ------------------------------------------------------------
# Logos
# ------------------------------------------------------------

for logo in logos:

    channel_id = logo.get(
        "channel"
    )

    logo_url = logo.get(
        "url"
    )

    if not channel_id:
        continue

    if not logo_url:
        continue

    if not logo.get(
        "in_use",
        True,
    ):
        continue

    if channel_id not in logos_by_channel:
        logos_by_channel[
            channel_id
        ] = logo_url


print(
    "Logos:",
    len(logos_by_channel),
)


# ------------------------------------------------------------
# Candidate streams
# ------------------------------------------------------------

candidates = []

for stream in streams:

    channel_id = stream.get(
        "channel"
    )

    if not channel_id:
        continue

    if channel_id not in channels_by_id:
        continue

    url = normalize_url(
        stream.get("url")
    )

    if not url:
        continue

    label = normalize_text(
        stream.get("label")
    )

    title = normalize_text(
        stream.get("title")
    )

    bad_markers = [
        "blocked",
        "dead",
        "offline",
        "geo-blocked",
        "geoblocked",
    ]

    combined = (
        label
        + " "
        + title
    )

    if any(
        marker in combined
        for marker in bad_markers
    ):
        continue

    # Поддерживаем только реальные web-stream URL
    if not (
        url.startswith("http://")
        or url.startswith("https://")
    ):
        continue

    candidates.append(
        stream
    )


# ------------------------------------------------------------
# Deduplicate
# ------------------------------------------------------------

unique_streams = {}

for stream in candidates:

    url = normalize_url(
        stream.get("url")
    )

    if not url:
        continue

    if url not in unique_streams:
        unique_streams[
            url
        ] = stream


candidates = list(
    unique_streams.values()
)


print()
print(
    "Candidate streams:",
    len(candidates),
)


# ------------------------------------------------------------
# Check streams
# ------------------------------------------------------------

working = []

total = len(
    candidates
)

checked = 0


print()
print(
    "Starting stream checks..."
)

print(
    "Workers:",
    WORKERS
)

print(
    "Timeout:",
    TIMEOUT_SECONDS,
    "seconds"
)

print()


with ThreadPoolExecutor(
    max_workers=WORKERS
) as executor:

    future_map = {
        executor.submit(
            check_stream,
            stream,
        ): stream

        for stream in candidates
    }

    for future in as_completed(
        future_map
    ):

        checked += 1

        try:

            result = future.result()

            if result:
                working.append(
                    result
                )

        except Exception:
            pass

        if (
            checked == 1
            or checked % 25 == 0
            or checked == total
        ):

            print(
                f"Progress: "
                f"{checked}/{total} "
                f"({checked * 100 / total:.1f}%) "
                f"working={len(working)}",
                flush=True,
            )


# ------------------------------------------------------------
# Group by channel
# ------------------------------------------------------------

streams_by_channel = {}

for stream in working:

    channel_id = stream.get(
        "channel"
    )

    if not channel_id:
        continue

    streams_by_channel.setdefault(
        channel_id,
        [],
    ).append(
        stream
    )


# ------------------------------------------------------------
# Sort streams
# ------------------------------------------------------------

for channel_id, items in (
    streams_by_channel.items()
):

    items.sort(
        key=stream_score,
        reverse=True,
    )


# ------------------------------------------------------------
# Select main streams
# ------------------------------------------------------------

selected = []

for channel_id, items in (
    streams_by_channel.items()
):

    selected.extend(
        items[
            :MAX_STREAMS_PER_CHANNEL
        ]
    )


# ------------------------------------------------------------
# BEST
# ------------------------------------------------------------

best = []

for channel_id, items in (
    streams_by_channel.items()
):

    if items:
        best.append(
            items[0]
        )


# ------------------------------------------------------------
# BACKUP
# ------------------------------------------------------------

backup = []

for channel_id, items in (
    streams_by_channel.items()
):

    if len(items) <= 1:
        continue

    backup.extend(
        items[
            1:
            1 + MAX_BACKUP_PER_CHANNEL
        ]
    )


# ------------------------------------------------------------
# Quality playlists
# ------------------------------------------------------------

hd = [
    stream
    for stream in working
    if stream.get(
        "_resolution",
        0,
    ) >= 720
]

fhd = [
    stream
    for stream in working
    if stream.get(
        "_resolution",
        0,
    ) >= 1080
]

uhd = [
    stream
    for stream in working
    if stream.get(
        "_resolution",
        0,
    ) >= 2160
]

hdr = [
    stream
    for stream in working
    if stream.get(
        "_hdr",
        False,
    )
]


# ------------------------------------------------------------
# Sort playlists
# ------------------------------------------------------------

selected.sort(
    key=stream_score,
    reverse=True,
)

best.sort(
    key=stream_score,
    reverse=True,
)

backup.sort(
    key=stream_score,
    reverse=True,
)

hd.sort(
    key=stream_score,
    reverse=True,
)

fhd.sort(
    key=stream_score,
    reverse=True,
)

uhd.sort(
    key=stream_score,
    reverse=True,
)

hdr.sort(
    key=stream_score,
    reverse=True,
)


# ------------------------------------------------------------
# Create playlists
# ------------------------------------------------------------

make_playlist(
    selected,
    "russia.m3u",
    "Russia",
)

make_playlist(
    hd,
    "russia-hd.m3u",
    "Russia HD",
)

make_playlist(
    fhd,
    "russia-fhd.m3u",
    "Russia FHD",
)

make_playlist(
    uhd,
    "russia-4k.m3u",
    "Russia 4K",
)

make_playlist(
    hdr,
    "russia-hdr.m3u",
    "Russia HDR",
)

make_playlist(
    best,
    "russia-best.m3u",
    "Russia BEST",
)

make_playlist(
    backup,
    "russia-backup.m3u",
    "Russia BACKUP",
)


# ------------------------------------------------------------
# Region statistics
# ------------------------------------------------------------

regions = {}

for stream in working:

    region = (
        stream.get(
            "_region_name"
        )
        or "Федеральные / неизвестно"
    )

    regions[region] = (
        regions.get(region, 0)
        + 1
    )


regions_sorted = dict(
    sorted(
        regions.items(),
        key=lambda item: item[1],
        reverse=True,
    )
)


# ------------------------------------------------------------
# Resolution statistics
# ------------------------------------------------------------

resolution_stats = {}

for stream in working:

    resolution = str(
        stream.get(
            "_resolution",
            0,
        )
    )

    resolution_stats[
        resolution
    ] = (
        resolution_stats.get(
            resolution,
            0,
        )
        + 1
    )


# ------------------------------------------------------------
# Codec statistics
# ------------------------------------------------------------

video_codecs = {}

for stream in working:

    codec = (
        stream.get(
            "_video_codec"
        )
        or "unknown"
    )

    video_codecs[codec] = (
        video_codecs.get(
            codec,
            0,
        )
        + 1
    )


video_codecs = dict(
    sorted(
        video_codecs.items(),
        key=lambda item: item[1],
        reverse=True,
    )
)


# ------------------------------------------------------------
# Report
# ------------------------------------------------------------

report = {
    "version": 4,

    "russian_channels":
        len(channels_by_id),

    "candidate_streams":
        len(candidates),

    "working_streams":
        len(working),

    "channels_with_working_stream":
        len(streams_by_channel),

    "selected_streams":
        len(selected),

    "best_streams":
        len(best),

    "backup_streams":
        len(backup),

    "hd_streams":
        len(hd),

    "fhd_streams":
        len(fhd),

    "4k_streams":
        len(uhd),

    "hdr_streams":
        len(hdr),

    "resolution_distribution":
        resolution_stats,

    "video_codecs":
        video_codecs,

    "regions":
        regions_sorted,
}


report_path = os.path.join(
    OUTPUT_DIR,
    "report.json",
)


with open(
    report_path,
    "w",
    encoding="utf-8",
) as file:

    json.dump(
        report,
        file,
        ensure_ascii=False,
        indent=2,
    )


# ------------------------------------------------------------
# Final output
# ------------------------------------------------------------

print()
print("=" * 70)
print("FINAL REPORT")
print("=" * 70)

for key, value in report.items():

    if key in (
        "resolution_distribution",
        "video_codecs",
        "regions",
    ):
        continue

    print(
        f"{key}: {value}"
    )

print()
print("Resolution:")
print(
    json.dumps(
        resolution_stats,
        ensure_ascii=False,
    )
)

print()
print("Video codecs:")
print(
    json.dumps(
        video_codecs,
        ensure_ascii=False,
    )
)

print()
print("Top regions:")

for name, count in list(
    regions_sorted.items()
)[:20]:

    print(
        f"  {name}: {count}"
    )

print()
print("=" * 70)
print("BUILD COMPLETE")
print("=" * 70)
