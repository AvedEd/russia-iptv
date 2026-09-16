import json
import os
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


# ============================================================
# RUSSIA IPTV AGGREGATOR V5
# ONLY 1080p+
# ============================================================

API_BASE = "https://iptv-org.github.io/api"
OUTPUT_DIR = "output"

WORKERS = 24
TIMEOUT_SECONDS = 8

# Минимальное реальное разрешение:
# 1080p и выше
MIN_HEIGHT = 1080

# Максимум потоков одного канала
MAX_STREAMS_PER_CHANNEL = 3

# Резервов после BEST
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
# LOAD JSON
# ============================================================

def load_json(name):
    url = f"{API_BASE}/{name}.json"

    print()
    print("=" * 70)
    print("Downloading:", url)

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=60
        ) as response:

            data = response.read()

        result = json.loads(data)

        print(
            "Loaded:",
            name,
            "items:",
            len(result),
        )

        return result

    except Exception as error:

        print(
            "ERROR loading",
            name,
            ":",
            error,
        )

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
# HDR
# ============================================================

def detect_hdr(video):

    values = []

    for key in (
        "color_transfer",
        "color_space",
        "color_primaries",
        "pix_fmt",
    ):

        value = video.get(key)

        if value:
            values.append(
                str(value).lower()
            )

    side_data = video.get(
        "side_data_list"
    )

    if side_data:
        values.append(
            str(side_data).lower()
        )

    text = " ".join(values)

    # PQ / HDR10
    if "smpte2084" in text:
        return True

    # HLG
    if "arib-std-b67" in text:
        return True

    # Dolby Vision
    if "dolby" in text:
        return True

    if "dovi" in text:
        return True

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
# REGION
# ============================================================

def get_region_info(feed_id):

    if not feed_id:
        return {
            "code": "",
            "name": "",
        }

    feed = feeds_by_id.get(
        feed_id
    )

    if not feed:
        return {
            "code": "",
            "name": "",
        }

    areas = feed.get(
        "broadcast_area"
    ) or []

    for area in areas:

        area = str(area)

        if area.startswith("s/"):

            code = area[2:]

            subdivision = (
                subdivisions_by_code.get(
                    code
                )
            )

            if subdivision:

                return {
                    "code": code,
                    "name": subdivision.get(
                        "name",
                        code,
                    ),
                }

            return {
                "code": code,
                "name": code,
            }

    return {
        "code": "",
        "name": "",
    }


# ============================================================
# FFPROBE
# ============================================================

def run_ffprobe(
    url,
    referrer=None,
    user_agent=None,
):

    command = [
        "ffprobe",

        "-v",
        "error",

        "-print_format",
        "json",

        "-show_streams",

        "-show_format",

        "-probesize",
        "2M",

        "-analyzeduration",
        "3M",

        "-rw_timeout",
        str(
            TIMEOUT_SECONDS
            * 1_000_000
        ),
    ]

    headers = []

    headers.append(
        "User-Agent: "
        + (
            user_agent
            if user_agent
            else USER_AGENT
        )
    )

    if referrer:

        headers.append(
            "Referer: "
            + str(referrer)
        )

    command.extend(
        [
            "-headers",
            "\r\n".join(headers)
            + "\r\n",
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

        return json.loads(
            result.stdout
        )

    except subprocess.TimeoutExpired:

        return None

    except json.JSONDecodeError:

        return None

    except Exception:

        return None


# ============================================================
# CHECK STREAM
# ============================================================

def check_stream(stream):

    url = normalize_url(
        stream.get("url")
    )

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

    for item in data.get(
        "streams",
        [],
    ):

        codec_type = item.get(
            "codec_type"
        )

        if (
            codec_type == "video"
            and video is None
        ):

            video = item

        elif (
            codec_type == "audio"
            and audio is None
        ):

            audio = item

    if not video:
        return None

    width = video.get(
        "width",
        0,
    )

    height = video.get(
        "height",
        0,
    )

    if not width or not height:
        return None

    # ========================================================
    # CRITICAL V5 FILTER
    #
    # Only 1080p+
    # ========================================================

    if int(height) < MIN_HEIGHT:
        return None

    resolution = get_resolution(
        width,
        height,
    )

    if resolution < MIN_HEIGHT:
        return None

    video_codec = (
        video.get(
            "codec_name"
        )
        or ""
    )

    audio_codec = ""

    if audio:

        audio_codec = (
            audio.get(
                "codec_name"
            )
            or ""
        )

    hdr = detect_hdr(
        video
    )

    region = get_region_info(
        stream.get("feed")
    )

    result = dict(stream)

    result["_online"] = True

    result["_width"] = int(width)
    result["_height"] = int(height)

    result["_resolution"] = resolution

    result["_video_codec"] = (
        video_codec
    )

    result["_audio_codec"] = (
        audio_codec
    )

    result["_hdr"] = hdr

    result["_region_code"] = (
        region["code"]
    )

    result["_region_name"] = (
        region["name"]
    )

    return result


# ============================================================
# SCORE
# ============================================================

def stream_score(stream):

    score = 0

    resolution = stream.get(
        "_resolution",
        0,
    )

    # Resolution dominates
    score += (
        resolution * 1000
    )

    # HDR
    if stream.get("_hdr"):

        score += 500

    codec = normalize_text(
        stream.get(
            "_video_codec"
        )
    )

    # Modern codecs preferred
    if codec == "av1":

        score += 100

    elif codec in (
        "hevc",
        "h265",
    ):

        score += 80

    elif codec in (
        "h264",
        "avc1",
    ):

        score += 40

    elif codec == "mpeg2video":

        score -= 100

    audio = normalize_text(
        stream.get(
            "_audio_codec"
        )
    )

    if audio in (
        "eac3",
        "ac3",
        "aac",
    ):

        score += 20

    # Prefer source entries with no warning
    label = normalize_text(
        stream.get(
            "label"
        )
    )

    if not label:

        score += 30

    # Prefer HTTPS
    url = normalize_url(
        stream.get("url")
    )

    if url.startswith(
        "https://"
    ):

        score += 10

    return score


# ============================================================
# CHANNEL NAME
# ============================================================

def get_channel_name(
    stream
):

    channel_id = stream.get(
        "channel"
    )

    channel = channels_by_id.get(
        channel_id,
        {},
    )

    return str(
        channel.get("name")
        or stream.get("title")
        or channel_id
        or "Unknown"
    )


# ============================================================
# PLAYLIST
# ============================================================

def make_playlist(
    streams,
    filename,
    playlist_name,
):

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
            "#EXTM3U\n"
        )

        for stream in streams:

            channel_id = stream.get(
                "channel"
            )

            if not channel_id:
                continue

            if channel_id not in (
                channels_by_id
            ):
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

            codec = stream.get(
                "_video_codec",
                "",
            )

            hdr = stream.get(
                "_hdr",
                False,
            )

            region = stream.get(
                "_region_name",
                "",
            )

            channel = channels_by_id.get(
                channel_id,
                {},
            )

            categories = (
                channel.get(
                    "categories"
                )
                or []
            )

            if categories:

                group = (
                    "Russia / "
                    + str(
                        categories[0]
                    )
                )

            else:

                group = "Russia"

            attributes = [
                f'tvg-id="{channel_id}"',
                f'tvg-name="{name}"',
                f'group-title="{group}"',
            ]

            if logo:

                attributes.append(
                    f'tvg-logo="{logo}"'
                )

            label = (
                f"{name} "
                f"[{width}x{height}]"
            )

            if codec:

                label += (
                    f" [{codec.upper()}]"
                )

            if hdr:

                label += " [HDR]"

            if region:

                label += (
                    f" [{region}]"
                )

            file.write(
                "#EXTINF:-1 "
                + " ".join(
                    attributes
                )
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
print("RUSSIA IPTV AGGREGATOR V5")
print("ONLY 1080p+")
print("=" * 70)
print()


# ============================================================
# LOAD API
# ============================================================

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


# ============================================================
# CHANNELS
# ============================================================

channels_by_id = {
    channel["id"]: channel
    for channel in channels
    if channel.get(
        "country"
    ) == "RU"
}

print(
    "Russian channels:",
    len(channels_by_id),
)


# ============================================================
# FEEDS
# ============================================================

feeds_by_id = {
    feed["id"]: feed
    for feed in feeds
    if feed.get("id")
}

print(
    "Feeds:",
    len(feeds_by_id),
)


# ============================================================
# SUBDIVISIONS
# ============================================================

subdivisions_by_code = {
    item["code"]: item
    for item in subdivisions
    if item.get("code")
}


# ============================================================
# LOGOS
# ============================================================

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

    if (
        channel_id
        not in logos_by_channel
    ):

        logos_by_channel[
            channel_id
        ] = logo_url


print(
    "Logos:",
    len(logos_by_channel),
)


# ============================================================
# CANDIDATES
# ============================================================

candidates = []

for stream in streams:

    channel_id = stream.get(
        "channel"
    )

    if not channel_id:
        continue

    if channel_id not in (
        channels_by_id
    ):
        continue

    url = normalize_url(
        stream.get("url")
    )

    if not url:
        continue

    if not (
        url.startswith(
            "http://"
        )
        or url.startswith(
            "https://"
        )
    ):

        continue

    label = normalize_text(
        stream.get("label")
    )

    title = normalize_text(
        stream.get("title")
    )

    quality = normalize_text(
        stream.get("quality")
    )

    combined = (
        label
        + " "
        + title
        + " "
        + quality
    )

    bad_markers = [
        "blocked",
        "dead",
        "offline",
        "geo-blocked",
        "geoblocked",
    ]

    if any(
        marker in combined
        for marker in bad_markers
    ):

        continue

    candidates.append(
        stream
    )


# ============================================================
# DEDUPLICATION
# ============================================================

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


print(
    "Candidate streams:",
    len(candidates),
)


# ============================================================
# FFPROBE
# ============================================================

working = []

total = len(
    candidates
)

checked = 0

print()
print(
    "Checking streams..."
)

print(
    "Minimum resolution:",
    "1080p",
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
                f"({checked * 100 / max(total, 1):.1f}%) "
                f"1080p+={
                    len(working)
                }",
                flush=True,
            )


# ============================================================
# GROUP BY CHANNEL
# ============================================================

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


# ============================================================
# SORT
# ============================================================

for channel_id, items in (
    streams_by_channel.items()
):

    items.sort(
        key=stream_score,
        reverse=True,
    )


# ============================================================
# SELECT
# ============================================================

selected = []

best = []

backup = []


for channel_id, items in (
    streams_by_channel.items()
):

    selected.extend(
        items[
            :MAX_STREAMS_PER_CHANNEL
        ]
    )

    if items:

        best.append(
            items[0]
        )

    if len(items) > 1:

        backup.extend(
            items[
                1:
                1 + MAX_BACKUP_PER_CHANNEL
            ]
        )


# ============================================================
# QUALITY
# ============================================================

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


# ============================================================
# SORT OUTPUT
# ============================================================

for playlist in (
    selected,
    best,
    backup,
    fhd,
    uhd,
    hdr,
):

    playlist.sort(
        key=stream_score,
        reverse=True,
    )


# ============================================================
# WRITE PLAYLISTS
# ============================================================

make_playlist(
    best,
    "russia-best.m3u",
    "Russia BEST 1080p+",
)

make_playlist(
    fhd,
    "russia-fhd.m3u",
    "Russia FHD+",
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
    backup,
    "russia-backup.m3u",
    "Russia BACKUP 1080p+",
)

# Основной список теперь тоже только 1080p+
make_playlist(
    selected,
    "russia.m3u",
    "Russia 1080p+",
)


# ============================================================
# RESOLUTION STATS
# ============================================================

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


# ============================================================
# CODEC STATS
# ============================================================

codec_stats = {}

for stream in working:

    codec = (
        stream.get(
            "_video_codec"
        )
        or "unknown"
    )

    codec_stats[
        codec
    ] = (
        codec_stats.get(
            codec,
            0,
        )
        + 1
    )


codec_stats = dict(
    sorted(
        codec_stats.items(),
        key=lambda item: item[1],
        reverse=True,
    )
)


# ============================================================
# REGION STATS
# ============================================================

region_stats = {}

for stream in working:

    region = (
        stream.get(
            "_region_name"
        )
        or "Федеральные / неизвестно"
    )

    region_stats[
        region
    ] = (
        region_stats.get(
            region,
            0,
        )
        + 1
    )


region_stats = dict(
    sorted(
        region_stats.items(),
        key=lambda item: item[1],
        reverse=True,
    )
)


# ============================================================
# REPORT
# ============================================================

report = {

    "version": 5,

    "filter": {
        "minimum_height": MIN_HEIGHT,
        "minimum_quality": "1080p",
    },

    "russian_channels":
        len(channels_by_id),

    "candidate_streams":
        len(candidates),

    "working_1080p_plus":
        len(working),

    "channels_with_1080p_plus":
        len(streams_by_channel),

    "selected_streams":
        len(selected),

    "best_streams":
        len(best),

    "backup_streams":
        len(backup),

    "fhd_streams":
        len(fhd),

    "4k_streams":
        len(uhd),

    "hdr_streams":
        len(hdr),

    "resolution_distribution":
        resolution_stats,

    "video_codecs":
        codec_stats,

    "regions":
        region_stats,
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


# ============================================================
# FINAL REPORT
# ============================================================

print()
print("=" * 70)
print("FINAL V5 REPORT")
print("=" * 70)

print(
    "Russian channels:",
    report[
        "russian_channels"
    ],
)

print(
    "Candidate streams:",
    report[
        "candidate_streams"
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
    "Selected:",
    report[
        "selected_streams"
    ],
)

print(
    "BEST:",
    report[
        "best_streams"
    ],
)

print(
    "BACKUP:",
    report[
        "backup_streams"
    ],
)

print(
    "FHD+:",
    report[
        "fhd_streams"
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

print()
print(
    "Resolution distribution:"
)

print(
    json.dumps(
        resolution_stats,
        ensure_ascii=False,
        indent=2,
    )
)

print()
print(
    "Video codecs:"
)

print(
    json.dumps(
        codec_stats,
        ensure_ascii=False,
        indent=2,
    )
)

print()
print("=" * 70)
print("BUILD COMPLETE")
print("=" * 70)
