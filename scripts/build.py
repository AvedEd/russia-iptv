import json
import os
import re
import urllib.request

BASE = "https://iptv-org.github.io/api"

OUT = "output"
os.makedirs(OUT, exist_ok=True)


def load(name):
    url = f"{BASE}/{name}.json"
    print("Loading:", url)

    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


channels = load("channels")
streams = load("streams")
logos = load("logos")

# Только российские каналы
ru_channels = {
    c["id"]: c
    for c in channels
    if c.get("country") == "RU"
}

print("Russian channels:", len(ru_channels))

# Логотипы
logo_map = {}

for logo in logos:
    channel = logo.get("channel")

    if channel and logo.get("url") and logo.get("in_use", True):
        if channel not in logo_map:
            logo_map[channel] = logo["url"]


def quality(stream):
    q = (stream.get("quality") or "").lower()

    m = re.search(r"(\d{3,4})p", q)

    if m:
        return int(m.group(1))

    if "4k" in q or "2160" in q or "uhd" in q:
        return 2160

    return 0


def make_playlist(items, filename):
    path = os.path.join(OUT, filename)

    with open(path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")

        for item in items:
            channel = item["channel"]
            ch = ru_channels[channel]

            name = ch.get("name", item.get("title", channel))
            url = item["url"]

            logo = logo_map.get(channel, "")

            q = quality(item)

            attrs = [
                f'tvg-id="{channel}"',
                f'tvg-name="{name}"',
                f'group-title="Russia"',
            ]

            if logo:
                attrs.append(f'tvg-logo="{logo}"')

            f.write(
                "#EXTINF:-1 "
                + " ".join(attrs)
                + f",{name} [{q}p]\n"
            )

            f.write(url + "\n")

    print(filename, ":", len(items))


# Собираем российские streams
ru_streams = []

for stream in streams:
    channel = stream.get("channel")
    url = stream.get("url")

    if not channel or not url:
        continue

    if channel not in ru_channels:
        continue

    # Не берем явно отмеченные проблемы
    label = (stream.get("label") or "").lower()

    if "geo-blocked" in label:
        continue

    if "blocked" in label:
        continue

    ru_streams.append(stream)


# Удаляем дубликаты URL
unique = {}

for stream in ru_streams:
    url = stream["url"]

    if url not in unique:
        unique[url] = stream

ru_streams = list(unique.values())


# Сортировка: сначала более высокое качество
ru_streams.sort(
    key=lambda x: quality(x),
    reverse=True
)


# Все доступные
make_playlist(
    ru_streams,
    "russia.m3u"
)


# HD+
hd = [
    x for x in ru_streams
    if quality(x) >= 720
]

make_playlist(
    hd,
    "russia-hd.m3u"
)


# Full HD+
fhd = [
    x for x in ru_streams
    if quality(x) >= 1080
]

make_playlist(
    fhd,
    "russia-fhd.m3u"
)


# 4K / UHD
uhd = [
    x for x in ru_streams
    if quality(x) >= 2160
]

make_playlist(
    uhd,
    "russia-4k.m3u"
)


print()
print("DONE")
