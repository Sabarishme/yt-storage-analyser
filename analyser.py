"""
analyser.py — YouTube Channel Storage Analyser
Core logic: fetch video metadata → classify → estimate storage → calibrate with yt-dlp (ALL videos)
"""

import os
import re
import time
import subprocess
import json
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────

BITRATE_TABLE = {
    "2160p": 8.0,
    "1440p": 5.0,
    "1080p": 2.5,
    "720p":  1.5,
    "480p":  0.8,
    "360p":  0.4,
}

RENDITION_MULTIPLIER = 3.8
AUDIO_BITRATE_KBPS   = 128
SHORTS_MAX_RESOLUTION      = "720p"
SHORTS_DURATION_THRESHOLD  = 60
YTDLP_TIMEOUT              = 30   # seconds per video
YTDLP_RETRY_DELAY          = 3    # seconds between retries on failure


# ─────────────────────────────────────────────
# YOUTUBE API CLIENT
# ─────────────────────────────────────────────

def get_youtube_client():
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        raise ValueError("YOUTUBE_API_KEY not set in .env")
    return build("youtube", "v3", developerKey=api_key)


# ─────────────────────────────────────────────
# CHANNEL RESOLVER
# ─────────────────────────────────────────────

def resolve_channel_id(input_str: str, youtube) -> str:
    s = input_str.strip()
    if re.match(r'^UC[\w-]{22}$', s):
        return s
    m = re.search(r'youtube\.com/channel/(UC[\w-]{22})', s)
    if m:
        return m.group(1)
    m = re.search(r'youtube\.com/@([\w.-]+)', s)
    handle = m.group(1) if m else (s.lstrip('@') if s.startswith('@') else None)
    if handle:
        resp = youtube.channels().list(part="id", forHandle=handle).execute()
        items = resp.get("items", [])
        if not items:
            raise ValueError(f"Channel not found for handle: @{handle}")
        return items[0]["id"]
    resp = youtube.search().list(part="snippet", q=s, type="channel", maxResults=1).execute()
    items = resp.get("items", [])
    if not items:
        raise ValueError(f"Could not resolve channel: {s}")
    return items[0]["snippet"]["channelId"]


# ─────────────────────────────────────────────
# VIDEO FETCHER
# ─────────────────────────────────────────────

def parse_iso8601_duration(duration_str: str) -> int:
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
    if not m:
        return 0
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)


def fetch_all_videos(channel_id: str, youtube) -> tuple[list[dict], dict]:
    ch_resp = youtube.channels().list(
        part="contentDetails,snippet", id=channel_id
    ).execute()
    ch_items = ch_resp.get("items", [])
    if not ch_items:
        raise ValueError(f"Channel not found: {channel_id}")

    channel_info = {
        "channel_id": channel_id,
        "channel_name": ch_items[0]["snippet"]["title"],
    }
    uploads_playlist = ch_items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    video_ids = []
    next_page = None
    while True:
        pl_resp = youtube.playlistItems().list(
            part="contentDetails", playlistId=uploads_playlist,
            maxResults=50, pageToken=next_page
        ).execute()
        for item in pl_resp.get("items", []):
            video_ids.append(item["contentDetails"]["videoId"])
        next_page = pl_resp.get("nextPageToken")
        if not next_page:
            break

    videos = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i+50]
        v_resp = youtube.videos().list(
            part="contentDetails,snippet,liveStreamingDetails",
            id=",".join(batch)
        ).execute()
        for item in v_resp.get("items", []):
            duration_sec = parse_iso8601_duration(
                item["contentDetails"].get("duration", "PT0S")
            )
            video = {
                "video_id":             item["id"],
                "title":                item["snippet"].get("title", "Unknown"),
                "duration_seconds":     duration_sec,
                "definition":           item["contentDetails"].get("definition", "sd"),
                "published_at":         item["snippet"].get("publishedAt", ""),
                "live_streaming_details": item.get("liveStreamingDetails", {}),
                "video_type":           None,
            }
            video["video_type"] = classify_video(video)
            videos.append(video)

    return videos, channel_info


def classify_video(video: dict) -> str:
    live = video.get("live_streaming_details", {})
    if live and live.get("actualEndTime"):
        return "live"
    if video["duration_seconds"] <= SHORTS_DURATION_THRESHOLD:
        return "short"
    return "video"


# ─────────────────────────────────────────────
# STORAGE ESTIMATOR
# ─────────────────────────────────────────────

def estimate_video_size_gb(video: dict, apply_rendition_multiplier=True) -> float:
    vtype      = video.get("video_type", "video")
    definition = video.get("definition", "sd")
    duration   = video["duration_seconds"]

    if vtype == "short":
        resolution = SHORTS_MAX_RESOLUTION
    elif definition == "hd":
        resolution = "1080p"
    else:
        resolution = "480p"

    bitrate       = BITRATE_TABLE[resolution]
    video_size_gb = (bitrate * duration) / 8 / 1024
    audio_size_gb = (AUDIO_BITRATE_KBPS / 1000 * duration) / 8 / 1024 * 2
    size_gb       = video_size_gb + audio_size_gb

    if apply_rendition_multiplier:
        size_gb *= RENDITION_MULTIPLIER
    return size_gb


def estimate_all_videos(videos: list[dict], correction_factors: dict) -> list[dict]:
    """Apply per-type correction factors to every video."""
    for v in videos:
        cf     = correction_factors.get(v["video_type"], correction_factors.get("overall", 1.0))
        single = estimate_video_size_gb(v, apply_rendition_multiplier=False) * cf
        total  = single * RENDITION_MULTIPLIER
        v["estimated_size_gb_single"] = round(single, 4)
        v["estimated_size_gb_total"]  = round(total,  4)
        v["correction_factor_used"]   = cf
    return videos


# ─────────────────────────────────────────────
# YT-DLP — CHECK ALL VIDEOS
# ─────────────────────────────────────────────

def run_ytdlp_json(video_id: str) -> dict | None:
    """Fetch format metadata for one video via yt-dlp (no download)."""
    url = f"https://youtube.com/watch?v={video_id}"
    try:
        result = subprocess.run(
            ["yt-dlp", "--dump-json", "--skip-download", url],
            capture_output=True, text=True, timeout=YTDLP_TIMEOUT
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return None


def get_real_size_gb(info: dict) -> float:
    """Extract best single-rendition size (video + audio) from yt-dlp info."""
    formats     = info.get("formats", [])
    video_sizes = {}
    audio_sizes = []

    for fmt in formats:
        vcodec = fmt.get("vcodec", "none")
        acodec = fmt.get("acodec", "none")
        fsize  = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        height = fmt.get("height") or 0
        if fsize == 0:
            continue
        if vcodec != "none" and acodec == "none" and height > 0:
            if height not in video_sizes or fsize > video_sizes[height]:
                video_sizes[height] = fsize
        if acodec != "none" and vcodec == "none":
            audio_sizes.append(fsize)

    best_video = max(video_sizes.values()) if video_sizes else 0
    best_audio = max(audio_sizes)          if audio_sizes else 0
    return (best_video + best_audio) / (1024 ** 3)


def check_all_with_ytdlp(videos: list[dict], progress_callback=None) -> dict:
    """
    Run yt-dlp on EVERY video one by one.
    Returns per-type correction factors:
      { "video": float, "short": float, "live": float, "overall": float }

    progress_callback(done, total, current_title) — called after each video.
    """
    factors_by_type = {"video": [], "short": [], "live": []}
    total = len(videos)

    for idx, v in enumerate(videos):
        info = run_ytdlp_json(v["video_id"])

        if info:
            real_gb      = get_real_size_gb(info)
            our_estimate = estimate_video_size_gb(v, apply_rendition_multiplier=False)
            if our_estimate > 0 and real_gb > 0:
                factors_by_type[v["video_type"]].append(real_gb / our_estimate)
        else:
            # Brief pause on failure to avoid hammering YouTube
            time.sleep(YTDLP_RETRY_DELAY)

        if progress_callback:
            progress_callback(idx + 1, total, v["title"])

    def avg(lst):
        if not lst:
            return 1.0
        s = sorted(lst)
        # Trim top/bottom 5% outliers
        trim = max(1, len(s) // 20)
        trimmed = s[trim:-trim] if len(s) > trim * 2 else s
        return round(sum(trimmed) / len(trimmed), 4)

    cf_video  = avg(factors_by_type["video"])
    cf_short  = avg(factors_by_type["short"])
    cf_live   = avg(factors_by_type["live"])

    all_factors = (
        factors_by_type["video"] +
        factors_by_type["short"] +
        factors_by_type["live"]
    )
    cf_overall = avg(all_factors)

    return {
        "video":   cf_video,
        "short":   cf_short,
        "live":    cf_live,
        "overall": cf_overall,
        "counts": {
            "video": len(factors_by_type["video"]),
            "short": len(factors_by_type["short"]),
            "live":  len(factors_by_type["live"]),
        }
    }


# ─────────────────────────────────────────────
# SUMMARY BUILDER
# ─────────────────────────────────────────────

def build_summary(videos: list[dict], channel_info: dict, correction_factors: dict) -> dict:
    regular = [v for v in videos if v["video_type"] == "video"]
    shorts  = [v for v in videos if v["video_type"] == "short"]
    live    = [v for v in videos if v["video_type"] == "live"]

    def total_size(lst):
        return round(sum(v["estimated_size_gb_total"] for v in lst), 2)

    videos_size   = total_size(regular)
    shorts_size   = total_size(shorts)
    live_size     = total_size(live)
    total_size_gb = round(videos_size + shorts_size + live_size, 2)

    top10 = sorted(videos, key=lambda v: v["estimated_size_gb_total"], reverse=True)[:10]

    cf_overall = correction_factors.get("overall", 1.0)
    deviation  = abs(cf_overall - 1.0) * 100
    if deviation < 20:
        accuracy = "±5–10%"
    elif deviation < 40:
        accuracy = "±10–20%"
    else:
        accuracy = "±20–30%"

    return {
        "channel_name":    channel_info["channel_name"],
        "channel_id":      channel_info["channel_id"],
        "total_videos":    len(regular),
        "total_shorts":    len(shorts),
        "total_live":      len(live),
        "total_size_gb":   total_size_gb,
        "videos_size_gb":  videos_size,
        "shorts_size_gb":  shorts_size,
        "live_size_gb":    live_size,
        "top10_biggest":   [
            {
                "video_id": v["video_id"],
                "title":    v["title"],
                "size_gb":  v["estimated_size_gb_total"],
                "type":     v["video_type"],
            }
            for v in top10
        ],
        "correction_factors":  correction_factors,
        "correction_factor":   cf_overall,
        "estimated_accuracy":  accuracy,
    }


# ─────────────────────────────────────────────
# STREAMING PIPELINE (called by app.py)
# ─────────────────────────────────────────────

def analyse_channel_stream(channel_input: str):
    """
    Generator that yields Server-Sent Event strings.
    Yields progress updates during yt-dlp phase,
    then yields the final result as a JSON event.
    """
    import json as _json

    def emit(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {_json.dumps(data)}\n\n"

    try:
        yield emit("status", {"msg": "Connecting to YouTube API…"})
        youtube = get_youtube_client()

        yield emit("status", {"msg": "Resolving channel…"})
        channel_id = resolve_channel_id(channel_input, youtube)

        yield emit("status", {"msg": "Fetching all video metadata…"})
        videos, channel_info = fetch_all_videos(channel_id, youtube)
        total = len(videos)
        yield emit("status", {"msg": f"Found {total} uploads. Starting yt-dlp check on ALL videos…"})

        # yt-dlp all videos with live progress
        results = {"factors": None}

        def progress_callback(done, total, title):
            pct = int(done / total * 100)
            yield_data = emit("progress", {
                "done":    done,
                "total":   total,
                "pct":     pct,
                "current": title[:60],
            })
            results["last_event"] = yield_data

        # We can't yield from inside a callback, so we collect events
        progress_events = []

        def cb(done, total, title):
            pct = int(done / total * 100)
            progress_events.append(emit("progress", {
                "done":    done,
                "total":   total,
                "pct":     pct,
                "current": title[:60],
            }))

        # Run synchronously, flushing events as they accumulate
        factors_result = {"value": None}

        def run_and_collect():
            factors_result["value"] = check_all_with_ytdlp(videos, progress_callback=cb)

        import threading
        t = threading.Thread(target=run_and_collect)
        t.start()

        last_sent = 0
        while t.is_alive():
            while last_sent < len(progress_events):
                yield progress_events[last_sent]
                last_sent += 1
            time.sleep(0.2)

        # Flush remaining
        while last_sent < len(progress_events):
            yield progress_events[last_sent]
            last_sent += 1

        t.join()
        correction_factors = factors_result["value"]

        yield emit("status", {"msg": "Applying corrections and building summary…"})
        videos = estimate_all_videos(videos, correction_factors)
        summary = build_summary(videos, channel_info, correction_factors)

        yield emit("result", summary)

    except Exception as e:
        yield emit("error", {"msg": str(e)})
