# Intelligence Briefings — Project Context for Claude

## What This Is
An AI-powered executive podcast platform hosted on Railway. It generates 5-10 minute audio briefings on topics relevant to a senior analytics/AI executive (Ed Dobbles, CAO at Overproof). Two AI hosts — Alex and Morgan — debate and analyze topics in a structured dialogue format. Episodes are published as an RSS feed consumable by Apple Podcasts or any podcast app.

**Production URL:** https://intelligence-briefings-production.up.railway.app  
**Local project path:** `C:\Users\eddob\Claude Projects\podcast-console\`  
**Railway project ID:** 8d9b2720-162c-45a4-a209-1af24d88583e

---

## Stack
- **Backend:** Python / Flask, deployed on Railway with gunicorn
- **TTS:** ElevenLabs (eleven_turbo_v2_5, mp3_44100_128)
- **Script generation:** Anthropic Claude API (claude-sonnet-4-20250514)
- **Data persistence:** Railway volume mounted at `~/Intelligence-Briefings/`
- **Frontend:** Single-page HTML/CSS/JS in `templates/index.html`
- **Process manager:** `Procfile` with gunicorn gthread workers, 300s timeout

## Key Files
```
podcast-console/
├── app.py                          # Full backend — all logic lives here
├── templates/index.html            # Single-page frontend
├── Procfile                        # gunicorn --workers 2 --worker-class gthread --threads 4 --timeout 300
├── requirements.txt
└── CLAUDE.md                       # This file
```

## Data Directory (Railway Volume)
```
~/Intelligence-Briefings/
├── episodes/                       # MP3 files + per-episode segment dirs
├── episodes.json                   # Episode metadata list
├── topics_cache.json               # Today's topics (refreshes daily)
├── jobs.json                       # Async job queue state
├── series.json                     # Series metadata
├── production_log.json             # Production count log
└── feed.xml                        # RSS feed (auto-rebuilt after each episode)
```

---

## Architecture: Async Job Queue

Railway has a 60-second HTTP proxy timeout. All generation tasks (2-4 minutes) run in background threads.

**Flow:**
1. Client POSTs to `/api/generate` or `/api/chat` → returns `{job_id, status: "queued"}` in <1 second
2. Background thread runs full pipeline, updates job status/progress in `jobs.json`
3. Client polls `GET /api/job/<job_id>` every 3 seconds
4. When `status === "done"`, result contains full episode object
5. UI renders audio player

**Job statuses:** `queued → running → done | error`

---

## Episode Generation Pipeline

1. `generate_grounded_script(topic, depth)` → calls Claude API → returns JSON array of `{host, text}` segments
2. `generate_episode_audio(client, script, ep_dir, voice_a_id, voice_b_id)` → calls ElevenLabs per segment → concatenates MP3s
3. Episode saved to `episodes.json`, RSS feed rebuilt automatically

**Segment targets (strictly enforced in prompt):**
- executive: minimum 10 segments
- standard: minimum 16 segments  
- deep: minimum 24 segments
- Each segment: 3-5 substantial sentences

**Web search:** Currently disabled (`use_web_search=False`) for reliability. Can re-enable once confirmed working — requires `anthropic-beta: web-search-2025-03-05` header.

---

## Features

### Today's Briefings
- 6 AI-generated topics refreshed daily (cached in `topics_cache.json`)
- Topics pulled from AnswerRocket AR dashboard + Claude editorial judgment
- Each card: Generate Briefing button + Create Series button

### Discover Tab
- Same 6 topics with expanded detail (core tension, why it matters, common mistake, sub-questions)
- Actions per topic: 90s Preview, Commission Episode, + Series, Dismiss

### Series Tab
- Create a multi-episode progressive arc (3, 6, 9, or 12 episodes)
- Input: topic card, free-text description, or article URL
- `generate_series_outline()` → Claude generates N-episode arc with episode titles/tensions
- Episodes produced sequentially in background thread
- Live progress tracking per episode in UI
- Series episodes tagged in Episodes tab

### Admin Tab
- **Queue management:** View active jobs, clear queued/errored jobs
- **RSS feed:** Force rebuild, copy URL
- **Auto-queue from AnswerRocket:** Pulls latest AR intelligence → generates topic → queues episode automatically

### Chat / Commission (Discover tab)
- Free-text input: topic description or article URL
- Claude generates topic object, then full episode
- Same async queue pattern

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Frontend |
| GET | `/feed.xml` | RSS podcast feed |
| GET | `/episodes/<filename>` | Serve MP3 |
| POST | `/api/generate` | Queue episode generation → returns `job_id` |
| POST | `/api/chat` | Queue chat-based generation → returns `job_id` |
| GET | `/api/job/<job_id>` | Poll job status |
| GET | `/api/queue` | List active jobs |
| POST | `/api/queue/clear` | Clear queued/errored jobs |
| POST | `/api/feed/rebuild` | Force RSS rebuild |
| POST | `/api/autoqueue` | Queue AR intelligence briefing |
| GET | `/api/series` | List all series |
| POST | `/api/series` | Create new series |
| GET | `/api/series/<series_id>` | Series status + per-episode job statuses |
| GET | `/api/topics` | Get today's topics |
| GET | `/api/episodes` | Get all episodes |
| GET | `/api/voices` | Get available ElevenLabs voices |

---

## Environment Variables (Railway)
```
ANTHROPIC_API_KEY=...
ELEVEN_LABS_API_KEY=...         # or ELEVENLABS_API_KEY
BASE_URL=https://intelligence-briefings-production.up.railway.app
DATA_DIR=                       # optional, defaults to ~/Intelligence-Briefings
```

---

## Known Issues / Next Priorities

1. **Web search disabled** — `use_web_search=False` in `generate_grounded_script()`. Episodes use Claude's training knowledge only. To re-enable: set `use_web_search=True` and verify Anthropic account has web search beta access.

2. **Series tested in code but not yet production-verified** — first end-to-end series run pending after this deploy.

3. **Auto-queue not on a schedule** — currently manual trigger only (Admin tab button). Could add cron-style trigger via Railway scheduled jobs if desired.

4. **Weekly cap set to 50** — raised from original 5 to accommodate series production volume.

5. **No episode deletion UI** — episodes accumulate. Manual cleanup requires editing `episodes.json` on Railway volume.

---

## Deploy Command
```powershell
cd "C:\Users\eddob\Claude Projects\podcast-console"
git add -A
git commit -m "your message"
railway up
```

## RSS Feed URL
```
https://intelligence-briefings-production.up.railway.app/feed.xml
```
Add this to Apple Podcasts → Listen Now → Add a Podcast by URL.
