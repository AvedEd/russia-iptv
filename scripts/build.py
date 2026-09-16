import json
import os
import re
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


# ============================================================
# CONFIGURATION
# ============================================================

API_BASE = "https://iptv-org.github.io/api"

OUTPUT_DIR = "output"

# Number of simultaneous ffprobe checks.
# 12 is deliberately conservative for GitHub Actions.
WORKERS = 12

# Timeout for one stream probe.
TIMEOUT_SECONDS = 15

# Maximum number of working streams kept for one channel.
MAX_STREAMS_PER_CHANNEL = 3

USER_AGENT = (
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "Chrome/120 Safari/537.36"
)


# ============================================================
# DIRECTORY
# ============================================================

os.makedirs(OUTPUT_DIR, exist_ok=True)


# ============================================================
# DOWNLOAD JSON
# ============================================================

def load_json(name):
    url = f"{API_BASE}/{name}.json"

    print()
    print("Downloading:")
    print(url)

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT
        }
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=60
        ) as response:

            data = response.read()

            return json.loads(data)

    except Exception as error:

        print(
            f"ERROR loading {name}.json:",
            error
        )

        raise


# ============================================================
# NORMALIZE URL
# ============================================================

def normalize_url(url):
    if not url:
        return ""

    url = url.strip()

    return url


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
            values.append(
                str(value).lower()
            )

    side_data = video_stream.get(
        "side_data_list"
    )

    if side_data:
        values.append(
            str(side_data).lower()
        )

    text = " ".join(values)

    markers = [
        "smpte2084",
        "arib-std-b67",
        "bt2020",
        "pq",
        "hlg",
        "dolby",
    ]

    return any(
        marker in text
        for marker in markers
    )


# ============================================================
# RESOLUTION
# ============================================================

def get_resolution(width, height):

    if not width or not height:
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
# FFPROBE
# ============================================================

def run_ffprobe(
    url,
    referrer=None,
    user_agent=None
):

    command = [
        "ffprobe",

        "-v",
        "error",

        "-print_format",
        "json",

        "-show_streams",

        "-show_format",

        "-rw_timeout",
        str(
            TIMEOUT_SECONDS * 1_000_000
        ),
    ]


    headers = []


    if user_agent:

        headers.append(
            f"User-Agent: {user_agent}"
        )

    else:

        headers.append(
            f"User-Agent: {USER_AGENT}"
        )


    if referrer:

        headers.append(
            f"Referer: {referrer}"
        )


    if headers:

        command.extend(
            [
                "-headers",
                "\r\n".join(headers)
                + "\r\n"
            ]
        )


    command.extend(
        [
            "-i",
            url
        ]
    )


    try:

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=TIMEOUT_SECONDS + 5
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
# CHECK ONE STREAM
# ============================================================

def check_stream(stream):

    url = normalize_url(
        stream.get("url")
    )

    if not url:

        return None


    print(
        "CHECK:",
        url
    )


    data = run_ffprobe(

        url,

        stream.get(
            "referrer"
        ),

        stream.get(
            "user_agent"
        )
    )


    if not data:

        print(
            "  DEAD"
        )

        return None


    video = None
    audio = None


    for item in data.get(
        "streams",
        []
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


    # A TV stream without video is not
    # useful for our playlist.

    if not video:

        print(
            "  NO VIDEO"
        )

        return None


    width = video.get(
        "width",
        0
    )

    height = video.get(
        "height",
        0
    )


    if not width or not height:

        print(
            "  UNKNOWN RESOLUTION"
        )

        return None


    resolution = get_resolution(
        width,
        height
    )


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


    result = dict(
        stream
    )


    result["_online"] = True

    result["_width"] = width

    result["_height"] = height

    result["_resolution"] = resolution

    result["_video_codec"] = (
        video_codec
    )

    result["_audio_codec"] = (
        audio_codec
    )

    result["_hdr"] = hdr


    print(
        "  OK:",
        f"{width}x{height}",
        video_codec,
        "HDR" if hdr else ""
    )


    return result


# ============================================================
# STREAM SCORE
# ============================================================

def stream_score(stream):

    score = 0


    # Resolution is the main factor.

    score += (
        stream.get(
            "_resolution",
            0
        )
        * 1000
    )


    # HDR gets a bonus.

    if stream.get(
        "_hdr",
        False
    ):

        score += 100


    # Prefer modern codecs.

    codec = (
        stream.get(
            "_video_codec",
            ""
        )
        .lower()
    )


    if codec in (
        "hevc",
        "h265"
    ):

        score += 30


    elif codec == "av1":

        score += 40


    elif codec == "h264":

        score += 10


    return score


# ============================================================
# CREATE M3U
# ============================================================

def make_playlist(
    streams,
    filename
):

    path = os.path.join(
        OUTPUT_DIR,
        filename
    )


    with open(
        path,
        "w",
        encoding="utf-8"
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


            channel = channels_by_id.get(
                channel_id
            )


            if not channel:

                continue


            name = (
                channel.get(
                    "name"
                )
                or stream.get(
                    "title"
                )
                or channel_id
            )


            logo = logos_by_channel.get(
                channel_id,
                ""
            )


            width = stream.get(
                "_width",
                0
            )

            height = stream.get(
                "_height",
                0
            )


            codec = stream.get(
                "_video_codec",
                ""
            )


            hdr = stream.get(
                "_hdr",
                False
            )


            tags = [
                f'tvg-id="{channel_id}"',
                f'tvg-name="{name}"',
                'group-title="Russia"',
            ]


            if logo:

                tags.append(
                    f'tvg-logo="{logo}"'
                )


            # Informative label.

            label = (
                f"{name} "
                f"[{width}x{height}]"
            )


            if codec:

                label += (
                    f" [{codec}]"
                )


            if hdr:

                label += (
                    " [HDR]"
                )


            file.write(
                "#EXTINF:-1 "
                + " ".join(tags)
                + ","
                + label
                + "\n"
            )


            # Stream URL.

            file.write(
                stream["url"]
                + "\n"
            )


    print()
    print(
        "Created:",
        filename,
        "streams:",
        len(streams)
    )


# ============================================================
# LOAD DATA
# ============================================================

print()
print(
    "========================================"
)

print(
    "RUSSIA IPTV AGGREGATOR v3"
)

print(
    "========================================"
)


channels = load_json(
    "channels"
)

streams = load_json(
    "streams"
)

logos = load_json(
    "logos"
)


# ============================================================
# RUSSIAN CHANNELS
# ============================================================

channels_by_id = {

    channel["id"]: channel

    for channel in channels

    if channel.get(
        "country"
    ) == "RU"
}


print()
print(
    "Russian channels:",
    len(channels_by_id)
)


# ============================================================
# LOGOS
# ============================================================

logos_by_channel = {}


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


    if (
        not logo.get(
            "in_use",
            True
        )
    ):

        continue


    if channel_id not in logos_by_channel:

        logos_by_channel[
            channel_id
        ] = logo_url


# ============================================================
# SELECT RUSSIAN STREAMS
# ============================================================

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
        stream.get(
            "url"
        )
    )


    if not url:

        continue


    label = (
        stream.get(
            "label"
        )
        or ""
    ).lower()


    # Ignore explicitly marked bad streams.

    bad_markers = [
        "blocked",
        "dead",
        "offline",
    ]


    if any(
        marker in label
        for marker in bad_markers
    ):

        continue


    candidates.append(
        stream
    )


# ============================================================
# DEDUPLICATE URLS
# ============================================================

unique_streams = {}


for stream in candidates:

    url = normalize_url(
        stream.get(
            "url"
        )
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
    len(candidates)
)


# ============================================================
# HEALTH CHECK
# ============================================================

working = []


print()
print(
    "Starting stream checks..."
)

print(
    "Workers:",
    WORKERS
)


with ThreadPoolExecutor(
    max_workers=WORKERS
) as executor:


    future_map = {

        executor.submit(
            check_stream,
            stream
        ): stream

        for stream in candidates

    }


    for future in as_completed(
        future_map
    ):

        try:

            result = future.result()


            if result:

                working.append(
                    result
                )


        except Exception as error:

            print(
                "CHECK ERROR:",
                error
            )


print()
print(
    "Working streams:",
    len(working)
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


    if channel_id not in streams_by_channel:

        streams_by_channel[
            channel_id
        ] = []


    streams_by_channel[
        channel_id
    ].append(
        stream
    )


# ============================================================
# SORT STREAMS
# ============================================================

for channel_id, items in (
    streams_by_channel.items()
):

    items.sort(
        key=stream_score,
        reverse=True
    )


# ============================================================
# SELECT BEST + BACKUPS
# ============================================================

selected = []


for channel_id, items in (
    streams_by_channel.items()
):

    selected.extend(
        items[
            :MAX_STREAMS_PER_CHANNEL
        ]
    )


print()
print(
    "Channels with working streams:",
    len(streams_by_channel)
)

print(
    "Selected streams:",
    len(selected)
)


# ============================================================
# HD
# ============================================================

hd = [

    stream

    for stream in working

    if stream.get(
        "_resolution",
        0
    ) >= 720

]


# ============================================================
# FULL HD
# ============================================================

fhd = [

    stream

    for stream in working

    if stream.get(
        "_resolution",
        0
    ) >= 1080

]


# ============================================================
# 4K / UHD
# ============================================================

uhd = [

    stream

    for stream in working

    if stream.get(
        "_resolution",
        0
    ) >= 2160

]


# ============================================================
# HDR
# ============================================================

hdr = [

    stream

    for stream in working

    if stream.get(
        "_hdr",
        False
    )

]


# ============================================================
# CREATE PLAYLISTS
# ============================================================

make_playlist(
    selected,
    "russia.m3u"
)


make_playlist(
    hd,
    "russia-hd.m3u"
)


make_playlist(
    fhd,
    "russia-fhd.m3u"
)


make_playlist(
    uhd,
    "russia-4k.m3u"
)


make_playlist(
    hdr,
    "russia-hdr.m3u"
)


# ============================================================
# REPORT
# ============================================================

report = {

    "version": 3,

    "russian_channels": (
        len(channels_by_id)
    ),

    "candidate_streams": (
        len(candidates)
    ),

    "working_streams": (
        len(working)
    ),

    "channels_with_working_stream": (
        len(streams_by_channel)
    ),

    "selected_streams": (
        len(selected)
    ),

    "hd_streams": (
        len(hd)
    ),

    "fhd_streams": (
        len(fhd)
    ),

    "4k_streams": (
        len(uhd)
    ),

    "hdr_streams": (
        len(hdr)
    ),

}


report_path = os.path.join(
    OUTPUT_DIR,
    "report.json"
)


with open(
    report_path,
    "w",
    encoding="utf-8"
) as file:

    json.dump(
        report,
        file,
        ensure_ascii=False,
        indent=2
    )


# ============================================================
# FINAL REPORT
# ============================================================

print()
print(
    "========================================"
)

print(
    "FINAL REPORT"
)

print(
    "========================================"
)

print(
    "Russian channels:",
    report["russian_channels"]
)

print(
    "Candidates:",
    report["candidate_streams"]
)

print(
    "Working:",
    report["working_streams"]
)

print(
    "Channels working:",
    report[
        "channels_with_working_stream"
    ]
)

print(
    "Selected:",
    report["selected_streams"]
)

print(
    "HD:",
    report["hd_streams"]
)

print(
    "FHD:",
    report["fhd_streams"]
)

print(
    "4K:",
    report["4k_streams"]
)

print(
    "HDR:",
    report["hdr_streams"]
)

print()
print(
    "BUILD COMPLETE"
)
