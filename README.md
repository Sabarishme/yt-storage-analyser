# YT Storage Analyser

Estimates how much YouTube storage a channel uses across Videos, Shorts, and Live streams.
Uses YouTube Data API v3 for bulk metadata + yt-dlp spot-checks for calibration.

---

## Tomorrow's Build Order

### 1. Setup (5 min)
```bash
cd yt-storage-analyser
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
cp env.example .env
```

### 2. Get YouTube API Key (5 min)
1. Go to https://console.cloud.google.com
2. New project → Enable "YouTube Data API v3"
3. Credentials → Create API Key
4. Paste into `.env`

### 3. Fill in analyser.py (core work)
Work top to bottom through the TODO comments:

| Function | What to do |
|---|---|
| `get_youtube_client()` | `return build("youtube", "v3", developerKey=api_key)` |
| `parse_iso8601_duration()` | Regex for PT#H#M#S |
| `resolve_channel_id()` | Handle UC..., @handle, full URLs |
| `fetch_all_videos()` | Paginate playlist, batch-fetch details |
| `classify_video()` | live → video → short rules |
| `estimate_video_size_gb()` | bitrate × duration / 8 / 1024 |
| `run_ytdlp_json()` | subprocess yt-dlp --dump-json |
| `spot_check_with_ytdlp()` | sample → compare → correction factor |
| `estimate_all_videos()` | apply correction to all |
| `build_summary()` | aggregate into final dict |
| `analyse_channel()` | wire the full pipeline |

### 4. Fill in app.py (10 min)
- Read JSON body, call `analyse_channel()`, return `jsonify()`

### 5. Fill in index.html (30 min)
- Style with dark YouTube theme
- Wire up `analyseChannel()` fetch call
- Chart.js pie + bar charts
- Summary cards + accuracy badge

### 6. Test
```bash
python app.py
# Open http://localhost:5000
# Try: @mkbhd, @MrBeast, @veritasium
```

---

## Storage Estimation Method

```
size_gb = (bitrate_mbps × duration_seconds) / 8 / 1024
total_size = size_gb × 2.7  # multi-rendition multiplier
```

Then corrected by yt-dlp spot-check factor on 7 random videos.

Expected accuracy: **±10–15%**
