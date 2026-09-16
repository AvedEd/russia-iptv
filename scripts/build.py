import json
import os
import re
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


API = "https://iptv-org.github.io/api"
OUTPUT = "output"

# Максимальное количество одновременных проверок
WORKERS = 12

# Таймаут одной проверки
TIMEOUT = 15

os.makedirs(OUTPUT, exist_ok=True)


def load_json(name):
    url = f"{API}/{name}.json"

    print(f"Downloading {url}")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Russia-IPTV-Aggregator/3.0"
        }
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def run_ffprobe(url, referrer=None, user_agent=None):
    cmd = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
    ]

    headers = []

    if user_agent:
        headers.append(f"User-Agent: {user_agent}")

    if referrer:
        headers.append(f"Referer: {referrer}")

    if headers:
        cmd += [
            "-headers",
            "".join(h + "\r\n" for h in headers)
        ]

    cmd += [
        "-rw_timeout",
        str(TIMEOUT * 1_000_000),
        "-i",
        url,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT + 5
        )

        if result.returncode != 0:
            return None

        return json.loads(result.stdout)

    except Exception:
        return None


def detect_hdr(stream):
    color_transfer = (
        stream.get("color_transfer") or ""
    ).lower()

    color_space = (
        stream.get("color_space") or ""
    ).lower()

    side_data = str(
        stream.get("side_data_list") or ""
    ).lower()

    text = (
        color_transfer
        + " "
        + color_space
        + " "
        + side_data
    )

    hdr_markers = [
        "smpte2084",
        "arib-std-b67",
        "bt2020",
        "pq",
        "hlg",
        "dolby",
    ]

    return any(x in text for x in hdr_markers)


def resolution(width, height):
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


def check_stream(stream):
    url = stream.get("url")

    if not url:
        return None

    print("CHECK:", url)

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
        if item.get("codec_type") == "video" and video is None:
            video = item

        if item.get("codec_type") == "audio" and audio is None:
            audio = item

    if not video:
        return None

    width = video.get("width", 0)
    height = video.get("height", 0)

    if not width or not height:
        return None

    codec = video.get("codec_name", "")

    hdr = detect_hdr(video)

    result = dict(stream)

    result["_online"] = True
    result["_width"] = width
    result["_height"] = height
    result["_resolution"] = resolution(width, height)
    result["_video_codec"] = codec
    result["_audio_codec"] = (
        audio.get("codec_name", "")
        if audio else ""
    )
    result["_hdr"] = hdr

    return result


def quality_score(stream):
    resolution_value = stream.get("_resolution", 0)

    hdr_bonus = 100 if stream.get("_hdr") else 0

    codec = stream.get("_video_codec", "").lower()

    codec_bonus = 20 if codec in (
        "hevc",
        "h265",
        "av1",
    ) else 0

    return (
        resolution_value * 1000
        + hdr_bonus
        + codec_bonus
    )


def make_playlist(streams, filename):
    path = os.path.join(OUTPUT, filename)

    with open(path, "w", encoding="utf-8") as f:

        f.write("#EXTM3U\n")

        for stream in streams:

            channel_id = stream["channel"]
            channel = channels_by_id[channel_id]

            name = channel.get(
                "name",
                stream.get("title", channel_id)
            )

            logo = logos_by_channel.get(channel_id, "")

            width = stream.get("_width", 0)
            height = stream.get("_height", 0)

            codec = stream.get("_video_codec", "")
            hdr = stream.get("_hdr", False)

            tags = [
                f'tvg-id="{channel_id}"',
                f'tvg-name="{name}"',
                'group-title="Russia"',
            ]

            if logo:
                tags.append(
                    f'tvg-logo="{logo}"'
                )

            if hdr:
                tags.append(
                    'video="HDR"'
                )

            label = (
                f"{name} "
                f"[{width}x{height}] "
                f"[{codec}]"
            )

            if hdr:
                label += " [HDR]"

            f.write(
                "#EXTINF:-1 "
                + " ".join(tags)
                + ","
                + label
                + "\n"
            )

            f.write(
                stream["url"]
                + "\n"
            )

    print(
        f"Created {filename}: {len(streams)} streams"
    )


# --------------------------------------------------
# LOAD DATA
# --------------------------------------------------

channels = load_json("channels")
streams = load_json("streams")
logos = load_json("logos")


channels_by_id = {
    x["id"]: x
    for x in channels
    if x.get("country") == "RU"
}


logos_by_channel = {}

for logo in logos:

    channel = logo.get("channel")

    if not channel:
        continue

    if not logo.get("in_use", True):
        continue

    url = logo.get("url")

    if url and channel not in logos_by_channel:
        logos_by_channel[channel] = url


print()
print(
    "Russian channels:",
    len(channels_by_id)
)


# --------------------------------------------------
# SELECT STREAMS
# --------------------------------------------------

candidates = []

for stream in streams:

    channel = stream.get("channel")

    if not channel:
        continue

    if channel not in channels_by_id:
        continue

    label = (
        stream.get("label") or ""
    ).lower()

    # Не включаем явно обозначенные проблемные ссылки
    if "geo-blocked" in label:
        continue

    if "blocked" in label:
        continue

    if "dead" in label:
        continue

    candidates.append(stream)


# Дедупликация URL

unique = {}

for stream in candidates:

    url = stream.get("url")

    if not url:
        continue

    if url not in unique:
        unique[url] = stream


candidates = list(unique.values())

print(
    "Streams to check:",
    len(candidates)
)


# --------------------------------------------------
# HEALTH CHECK
# --------------------------------------------------

working = []

with ThreadPoolExecutor(
    max_workers=WORKERS
) as executor:

    futures = [
        executor.submit(
            check_stream,
            stream
        )
        for stream in candidates
    ]

    for future in as_completed(futures):

        result = future.result()

        if result:
            working.append(result)


print()
print(
    "Working streams:",
    len(working)
)


# --------------------------------------------------
# BEST STREAM PER CHANNEL
# --------------------------------------------------

best_by_channel = {}

for stream in working:

    channel = stream["channel"]

    old = best_by_channel.get(channel)

    if not old:
        best_by_channel[channel] = stream
        continue

    if quality_score(stream) > quality_score(old):
        best_by_channel[channel] = stream


best = list(best_by_channel.values())


# --------------------------------------------------
# PLAYLISTS
# --------------------------------------------------

make_playlist(
    best,
    "russia.m3u"
)


hd = [
    x for x in working
    if x["_resolution"] >= 720
]

make_playlist(
    hd,
    "russia-hd.m3u"
)


fhd = [
    x for x in working
    if x["_resolution"] >= 1080
]

make_playlist(
    fhd,
    "russia-fhd.m3u"
)


uhd = [
    x for x in working
    if x["_resolution"] >= 2160
]

make_playlist(
    uhd,
    "russia-4k.m3u"
)


hdr = [
    x for x in working
    if x["_hdr"]
]

make_playlist(
    hdr,
    "russia-hdr.m3u"
)


# --------------------------------------------------
# REPORT
# --------------------------------------------------

report = {
    "channels": len(channels_by_id),
    "streams_checked": len(candidates),
    "streams_working": len(working),
    "channels_with_working_stream": len(best),
    "hd": len(hd),
    "fhd": len(fhd),
    "4k": len(uhd),
    "hdr": len(hdr),
}


with open(
    os.path.join(OUTPUT, "report.json"),
    "w",
    encoding="utf-8"
) as f:

    json.dump(
        report,
        f,
        ensure_ascii=False,
        indent=2
    )


print()
print("========== REPORT ==========")

for key, value in report.items():
    print(
        f"{key}: {value}"
    )

print()
print("DONE")
