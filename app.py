import os
import json
import shutil
import threading
import uuid
from pathlib import Path
from datetime import datetime, timezone, date, timedelta
from flask import Flask, jsonify, request, send_from_directory, render_template

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", Path.home() / "Intelligence-Briefings"))
EPISODES_DIR = DATA_DIR / "episodes"
TOPICS_CACHE = DATA_DIR / "topics_cache.json"
FEED_FILE = DATA_DIR / "feed.xml"
EPISODES_JSON = DATA_DIR / "episodes.json"
PRODUCTION_LOG = DATA_DIR / "production_log.json"
JOBS_FILE = DATA_DIR / "jobs.json"
SERIES_FILE = DATA_DIR / "series.json"
ENGAGEMENT_LOG = DATA_DIR / "engagement_log.json"

# ---------------------------------------------------------------------------
# ENGAGEMENT LOG
# ---------------------------------------------------------------------------

_engagement_lock = threading.Lock()

def load_engagement():
    if not ENGAGEMENT_LOG.exists():
        return []
    try:
        return json.loads(ENGAGEMENT_LOG.read_text())
    except Exception:
        return []

def save_engagement_event(event_type, topic_title, episode_id=None, pct=None, extra=None):
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "topic_title": topic_title,
    }
    if episode_id: event["episode_id"] = episode_id
    if pct is not None: event["pct"] = pct
    if extra: event.update(extra)
    with _engagement_lock:
        events = load_engagement()
        events.append(event)
        if len(events) > 2000:
            events = events[-2000:]
        ENGAGEMENT_LOG.write_text(json.dumps(events, indent=2))

def get_engagement_summary():
    events = load_engagement()
    if not events:
        return {}

    from collections import defaultdict
    topic_signals = defaultdict(lambda: {
        "previewed": False, "commissioned": False, "dismissed": False,
        "max_pct": 0, "listen_complete": False
    })

    for e in events:
        t = e.get("topic_title", "")
        if not t: continue
        et = e.get("event_type", "")
        if et == "preview_started":
            topic_signals[t]["previewed"] = True
        elif et == "commissioned":
            topic_signals[t]["commissioned"] = True
        elif et == "dismissed":
            topic_signals[t]["dismissed"] = True
        elif et == "play_pct":
            topic_signals[t]["max_pct"] = max(topic_signals[t]["max_pct"], e.get("pct", 0))
        elif et == "listen_complete":
            topic_signals[t]["listen_complete"] = True
            topic_signals[t]["max_pct"] = 100

    strong_interest = [t for t, s in topic_signals.items()
                       if s["listen_complete"] or s["max_pct"] >= 75]
    moderate_interest = [t for t, s in topic_signals.items()
                         if not s["dismissed"] and (s["previewed"] or s["max_pct"] >= 25)
                         and t not in strong_interest]
    dismissed = [t for t, s in topic_signals.items() if s["dismissed"]]

    return {
        "strong_interest": strong_interest[-20:],
        "moderate_interest": moderate_interest[-20:],
        "dismissed": dismissed[-30:],
    }

ELEVEN_API_KEY = os.environ.get("ELEVEN_LABS_API_KEY") or os.environ.get("ELEVENLABS_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
WEEKLY_CAP = 50

BASE_URL = os.environ.get("BASE_URL", "https://intelligence-briefings-production.up.railway.app")

for d in [DATA_DIR, EPISODES_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# JOB QUEUE
# ---------------------------------------------------------------------------

_jobs_lock = threading.Lock()

def _load_jobs():
    if not JOBS_FILE.exists():
        return {}
    try:
        return json.loads(JOBS_FILE.read_text())
    except Exception:
        return {}

def _save_jobs(jobs):
    if len(jobs) > 200:
        sorted_keys = sorted(jobs, key=lambda k: jobs[k].get("created_at", ""))
        for k in sorted_keys[:-200]:
            del jobs[k]
    JOBS_FILE.write_text(json.dumps(jobs, indent=2))

def create_job(job_type="generate", series_id=None, series_ep=None):
    job_id = str(uuid.uuid4())[:8]
    job = {
        "id": job_id,
        "type": job_type,
        "status": "queued",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "progress": "Queued...",
        "result": None,
        "error": None,
        "series_id": series_id,
        "series_ep": series_ep,
    }
    with _jobs_lock:
        jobs = _load_jobs()
        jobs[job_id] = job
        _save_jobs(jobs)
    return job_id

def update_job(job_id, **kwargs):
    kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
    with _jobs_lock:
        jobs = _load_jobs()
        if job_id in jobs:
            jobs[job_id].update(kwargs)
            _save_jobs(jobs)

def get_job(job_id):
    with _jobs_lock:
        return _load_jobs().get(job_id)

def get_all_jobs():
    with _jobs_lock:
        return _load_jobs()

def clear_queue():
    with _jobs_lock:
        jobs = _load_jobs()
        cleared = 0
        for jid in list(jobs.keys()):
            if jobs[jid]["status"] in ("queued", "error"):
                del jobs[jid]
                cleared += 1
        _save_jobs(jobs)
    return cleared

# ---------------------------------------------------------------------------
# PRODUCTION CAP
# ---------------------------------------------------------------------------

def get_production_log():
    if not PRODUCTION_LOG.exists():
        return []
    try:
        return json.loads(PRODUCTION_LOG.read_text())
    except Exception:
        return []

def log_production():
    log = get_production_log()
    log.append(datetime.now(timezone.utc).isoformat())
    PRODUCTION_LOG.write_text(json.dumps(log))

def productions_this_week():
    log = get_production_log()
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    return sum(1 for ts in log if datetime.fromisoformat(ts) > week_ago)

# ---------------------------------------------------------------------------
# EDITORIAL PROMPT
# ---------------------------------------------------------------------------

EDITORIAL_PROMPT = """You are an executive editorial strategist and systems thinker.
Propose high-leverage editorial topics for a senior analytics and AI executive.

CONTEXT:
- Former VP Advanced Analytics at Diageo North America (global CPG)
- Chief Analytics Officer at Overproof (beverage alcohol market intelligence, B Corp)
- Building multi-agent AI systems: governance, deterministic workflows, ROI measurement
- Enterprise AI deals with Heineken, Beam Suntory, Diageo
- Active C-suite job seeker (CAO / CDO / VP Analytics)
- Thinks in systems, incentives, capital allocation, and moats

CONSTRAINTS:
- No generic "AI is transforming everything" topics
- No beginner tutorials or LinkedIn listicles
- Prioritize depth, tension, decision leverage
- Each topic must feel slightly uncomfortable or contrarian
- Focus: enterprise AI, governance, economic models, org behavior, beverage alcohol

Generate 12 candidates internally. Return the top 6 ranked by intellectual upside, strategic leverage, and market relevance (next 2-3 years).

Return ONLY a valid JSON array of 6 objects, no markdown:
[{
  "rank": 1,
  "title": "provocative but professional title",
  "tension": "core contrarian thesis in 1-2 sentences",
  "why_it_matters": "strategic importance in 1 sentence",
  "common_mistake": "what sophisticated leaders get wrong in 1-2 sentences",
  "sub_questions": ["question 1", "question 2", "question 3"],
  "trailer_hook": "3-4 sentence spoken-word hook, direct to a peer executive"
}]"""

FALLBACK_TOPICS = [
    {"rank":1,"title":"The Governance Tax: Why Most Enterprise AI Programs Are Paying for Risk They've Already Accepted","tension":"Organizations build elaborate AI governance frameworks after already deploying high-risk systems. The governance comes after the exposure, not before it.","why_it_matters":"Misaligned governance timing creates compliance theater that burns budget without reducing actual risk.","common_mistake":"Leaders treat governance as a launch gate rather than a continuous risk calibration process, which means controls are always one deployment behind actual exposure.","sub_questions":["At what point does governance reduce risk versus just document it?","How do you price retroactive governance versus pre-deployment friction?","What incentive structures cause governance teams to prioritize documentation over risk reduction?"],"trailer_hook":"Here's something nobody in your governance steering committee wants to say out loud: most enterprise AI governance programs are retroactive. You've already deployed the models. You've already accepted the risk. The frameworks you're building now are documentation for decisions already made. The real question is whether your governance creates actual risk reduction or just paper trails."},
    {"rank":2,"title":"Agentic AI Broke Your ROI Model - And Your CFO Doesn't Know It Yet","tension":"Traditional ROI frameworks measure discrete outputs. Agentic AI generates value through non-linear, compounding processes largely invisible to standard measurement.","why_it_matters":"Executives who can't articulate agentic AI ROI in CFO terms will lose the budget war.","common_mistake":"Most analytics leaders retrofit agentic AI value into hours-saved metrics, which systematically undervalues compounding effects and undermines the investment case.","sub_questions":["What's the right unit of measurement for a system that improves its own decision quality over time?","How do you present agentic ROI to a CFO trained on capital budgeting?","What's the opportunity cost of NOT deploying agents while competitors do?"],"trailer_hook":"You cannot measure agentic AI the way you measured your last analytics platform. The value is in the loops - decisions made faster, signals never caught, systems learning at 3am. Your current ROI model was built for batch reporting. If you're still presenting AI value as hours-saved, you're losing the budget argument before it starts."},
    {"rank":3,"title":"The Data Moat Is Dead - What Replaces It as Strategic Advantage","tension":"For a decade, proprietary data was the defensible edge. Foundation models have commoditized data advantage faster than most executives have internalized.","why_it_matters":"Executives investing in data hoarding instead of workflow integration are building walls around empty vaults.","common_mistake":"Leaders conflate data volume with data advantage, not recognizing scarcity has shifted from data to operational judgment.","sub_questions":["What does a defensible moat look like when foundation models approximate your proprietary knowledge?","How do you communicate the shift from data strategy to workflow strategy to a board that funded the data lake?","Where does first-party behavioral data still create genuine asymmetry?"],"trailer_hook":"The data moat argument used to work. You had the data, competitors didn't, you had the edge. That logic is collapsing. When foundation models synthesize industry knowledge from public sources that rivals your proprietary training data, the moat isn't the data. The moat is the workflow."},
    {"rank":4,"title":"Beverage Alcohol's Data Silence Problem: Why the Industry Knows Less Than It Should","tension":"Despite massive distribution networks and decades of sell-through data, beverage alcohol remains one of the most information-asymmetric industries in CPG - by design.","why_it_matters":"The next competitive wave belongs to operators who solve the last-mile data problem, not those who spend more on brand.","common_mistake":"Brand teams treat the data gap as a vendor problem when the actual barrier is three-tier incentive misalignment no data provider can fix.","sub_questions":["What would real-time venue-level visibility require in a three-tier system?","Where does menu scraping create actionable intelligence that replaces missing sell-through data?","What's the strategic value of knowing venue penetration before competitors do?"],"trailer_hook":"The beverage alcohol industry sits on a paradox. Trillion-dollar brands. Global distribution. And almost no reliable real-time data on what's happening at venue level. The three-tier system was designed to create information asymmetry. That changes when AI reads menus at scale."},
    {"rank":5,"title":"Why Your Best Analysts Are Training Their Own Replacements","tension":"High-performing analysts who adopt AI are simultaneously commoditizing their own skills and becoming the most irreplaceable people in the organization.","why_it_matters":"Analytics talent strategy needs a complete rethink as the skill premium shifts from technical execution to system design.","common_mistake":"Analytics leaders protect headcount by resisting AI adoption, creating conditions for their function to be outsourced once leadership runs the math on AI-enabled generalists.","sub_questions":["What's the right ratio of AI-augmented analysts to traditional FTEs?","What skills are you hiring for in 2026 that didn't exist as a category in 2022?","How do you restructure performance management when AI handles most measurable output?"],"trailer_hook":"Your best analyst just used Claude to do in 20 minutes what used to take two weeks. You've repriced their labor market value downward and upward simultaneously. The person who knows how to direct AI toward the right problem is extraordinarily rare. How you respond to that tension will determine whether your analytics function compounds or collapses."},
    {"rank":6,"title":"The CAO Role Is Disappearing - What Comes Next Is More Powerful and Harder to Fill","tension":"The Chief Analytics Officer title is being absorbed into CAIO, CDO, and CTO roles - but the executive who translates AI capability into business strategy has never been more scarce.","why_it_matters":"Analytics leaders who define themselves by function rather than strategic value will find their seats eliminated in the next org redesign.","common_mistake":"CAOs defend their role by proving team output rather than positioning themselves as the interpreter between AI capability and board-level strategy.","sub_questions":["What's the actual job description of the executive who owns AI strategy in a post-CAO structure?","How do you transition from functional leader to strategic interpreter before the title disappears?","How do you build the board relationship that makes you essential regardless of title?"],"trailer_hook":"The CAO title is getting squeezed from three directions - Chief AI Officers taking the forward mandate, CDOs absorbing governance, CTOs claiming infrastructure. If your value proposition is 'I run the analytics function,' that's a shrinking job. If it's 'I make AI investments legible to the board,' that role has never been more critical or more vacant."}
]

AR_DASHBOARD_URL = "https://ar-intelligence-dashboard-production.up.railway.app/"

def fetch_ar_intelligence():
    try:
        import urllib.request
        from html.parser import HTMLParser
        req = urllib.request.Request(AR_DASHBOARD_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; IntelligenceBriefings/1.0)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.text_parts = []
                self.skip_tags = {"script", "style", "head"}
                self._skip = 0
            def handle_starttag(self, tag, attrs):
                if tag in self.skip_tags: self._skip += 1
            def handle_endtag(self, tag):
                if tag in self.skip_tags and self._skip > 0: self._skip -= 1
            def handle_data(self, data):
                if self._skip == 0:
                    s = data.strip()
                    if s: self.text_parts.append(s)
        parser = TextExtractor()
        parser.feed(html)
        raw = "\n".join(parser.text_parts)
        sections = {}
        if "Executive Summary" in raw:
            s = raw.find("Executive Summary") + len("Executive Summary")
            e = raw.find("Dominant strategic positions", s)
            sections["executive_summary"] = raw[s:e].strip()[:800]
        if "Dominant strategic positions" in raw:
            s = raw.find("Dominant strategic positions")
            e = raw.find("Strategic contradictions", s)
            sections["positions"] = raw[s:e].strip()[:1500]
        if "Strategic contradictions" in raw:
            s = raw.find("Strategic contradictions")
            e = raw.find("Questions that demonstrate", s)
            sections["tensions"] = raw[s:e].strip()[:1000]
        return sections
    except Exception as e:
        print(f"AR fetch failed: {e}")
        return {}

def call_anthropic(messages, max_tokens=2500, use_web_search=False):
    import urllib.request
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": max_tokens,
        "messages": messages
    }
    if use_web_search:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
    payload = json.dumps(body).encode()
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if use_web_search:
        headers["anthropic-beta"] = "web-search-2025-03-05"
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers=headers
    )
    timeout = 300 if use_web_search else 90
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())

def extract_text(data):
    return " ".join(b["text"] for b in data.get("content", []) if b.get("type") == "text").strip()

# ---------------------------------------------------------------------------
# TOPIC GENERATION
# ---------------------------------------------------------------------------

def get_topics_for_today():
    today = date.today().isoformat()
    if TOPICS_CACHE.exists():
        try:
            cached = json.loads(TOPICS_CACHE.read_text())
            if cached.get("date") == today:
                return cached["topics"]
        except Exception:
            pass
    topics = generate_topics_via_claude()
    TOPICS_CACHE.write_text(json.dumps({"date": today, "topics": topics}, indent=2))
    return topics

def generate_topics_via_claude():
    if not ANTHROPIC_API_KEY:
        return FALLBACK_TOPICS
    try:
        ar_data = fetch_ar_intelligence()
        prompt = EDITORIAL_PROMPT
        if ar_data:
            parts = [f"{k.upper()}:\n{v}" for k, v in ar_data.items()]
            prompt += "\n\nLIVE COMPETITIVE INTELLIGENCE (AnswerRocket):\n" + "\n\n".join(parts)
        data = call_anthropic([{"role": "user", "content": prompt}], max_tokens=2500)
        text = extract_text(data).strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"): text = text[4:]
        return json.loads(text)[:6]
    except Exception as e:
        print(f"Topic gen failed: {e}")
        return FALLBACK_TOPICS

# ---------------------------------------------------------------------------
# AUTO-QUEUE FROM ANSWERROCKET
# ---------------------------------------------------------------------------

def autoqueue_ar_topic(voice_alex, voice_morgan):
    try:
        ar_data = fetch_ar_intelligence()
        if not ar_data:
            print("[AUTOQUEUE] No AR data available")
            return None

        ar_text = "\n\n".join(f"{k.upper()}:\n{v}" for k, v in ar_data.items())
        prompt = f"""You are an editorial producer. Based on this live competitive intelligence,
generate the single most actionable topic for an executive analytics podcast.

{ar_text}

Return a SINGLE topic as valid JSON (no markdown):
{{
  "rank": 1,
  "title": "provocative but professional title",
  "tension": "core contrarian thesis in 1-2 sentences",
  "why_it_matters": "strategic importance in 1 sentence",
  "common_mistake": "what sophisticated leaders get wrong",
  "sub_questions": ["question 1", "question 2", "question 3"],
  "trailer_hook": "3-4 sentence spoken-word hook",
  "production_brief": "what makes this AR-specific and timely"
}}"""

        data = call_anthropic([{"role": "user", "content": prompt}], max_tokens=1000)
        text = extract_text(data).strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"): text = text[4:]
        topic = json.loads(text)
        topic["title"] = "[AR] " + topic["title"]

        job_id = create_job("generate")
        threading.Thread(
            target=_run_generate,
            args=(job_id, topic, "standard", voice_alex, voice_morgan, False,
                  topic.get("production_brief", "")),
            daemon=True
        ).start()
        print(f"[AUTOQUEUE] Queued AR topic: {topic['title']} -> job {job_id}")
        return job_id
    except Exception as e:
        print(f"[AUTOQUEUE] Failed: {e}")
        return None

# ---------------------------------------------------------------------------
# SERIES GENERATION
# ---------------------------------------------------------------------------

def load_series():
    if not SERIES_FILE.exists(): return []
    try: return json.loads(SERIES_FILE.read_text())
    except: return []

def save_series(series_list):
    SERIES_FILE.write_text(json.dumps(series_list, indent=2))

def generate_series_outline(topic_or_prompt, num_episodes=6):
    if isinstance(topic_or_prompt, dict):
        seed = f"TOPIC: {topic_or_prompt['title']}\nTENSION: {topic_or_prompt.get('tension','')}"
    else:
        seed = f"PROMPT/SOURCE: {topic_or_prompt}"

    prompt = f"""You are an executive podcast series producer.
Create a {num_episodes}-episode deep-dive series arc for a senior analytics and AI executive.

SEED:
{seed}

Design a progressive series where each episode builds on the previous.
Episode 1 = executive overview (the what and why).
Episodes 2-{num_episodes-1} = progressively deeper angles (mechanisms, case studies, frameworks, edge cases, implications).
Episode {num_episodes} = synthesis and forward view (what to do, what comes next).

Each episode must stand alone AND reward listeners who follow the arc.

Return ONLY a valid JSON array of {num_episodes} topic objects, no markdown:
[{{
  "episode_number": 1,
  "title": "specific episode title",
  "tension": "core thesis for this episode in 1-2 sentences",
  "why_it_matters": "strategic importance in 1 sentence",
  "common_mistake": "what leaders get wrong on this specific angle",
  "sub_questions": ["question 1", "question 2", "question 3"],
  "trailer_hook": "3-4 sentence spoken-word hook",
  "series_context": "1-2 sentences: how this episode fits the arc and what came before"
}}]"""

    try:
        data = call_anthropic([{"role": "user", "content": prompt}], max_tokens=4000)
        text = extract_text(data).strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"): text = text[4:]
        episodes = json.loads(text)
        print(f"[SERIES] Generated {len(episodes)}-episode arc")
        return episodes
    except Exception as e:
        print(f"[SERIES] Outline generation failed: {e}")
        raise

def _run_series(series_id, episodes, voice_alex, voice_morgan):
    series_list = load_series()
    series_entry = next((s for s in series_list if s["id"] == series_id), None)
    if not series_entry:
        return

    for i, ep_topic in enumerate(episodes):
        ep_num = i + 1
        job_id = series_entry["job_ids"][i]

        try:
            update_job(job_id, status="running",
                       progress=f"Episode {ep_num}/{len(episodes)}: Writing script...")

            production_brief = ep_topic.get("series_context", "")
            if ep_num > 1:
                production_brief += f" This is episode {ep_num} of {len(episodes)} in the series - assume listeners heard previous episodes."

            script, sources = generate_grounded_script(ep_topic, depth="standard",
                                                        production_brief=production_brief)
            log_production()

            update_job(job_id, progress=f"Episode {ep_num}/{len(episodes)}: Generating audio ({len(script)} segments)...")
            client = get_elevenlabs_client()
            voice_a_id = resolve_voice(client, voice_alex)
            voice_b_id = resolve_voice(client, voice_morgan)
            if not voice_a_id or not voice_b_id:
                update_job(job_id, status="error", error="Voice not found")
                continue

            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            ep_id = f"series-{series_id}-ep{ep_num}-{timestamp}"
            ep_dir = EPISODES_DIR / ep_id
            final_path = generate_episode_audio(client, script, ep_dir, voice_a_id, voice_b_id)
            dest = EPISODES_DIR / f"{ep_id}.mp3"
            shutil.copy2(final_path, dest)

            entry = {
                "id": ep_id,
                "title": f"[S: {series_entry['title']}] Ep {ep_num}: {ep_topic['title']}",
                "description": ep_topic.get("tension", ""),
                "file": f"{ep_id}.mp3",
                "file_size": dest.stat().st_size,
                "depth": "Standard",
                "is_trailer": False,
                "sources": sources,
                "published": datetime.now(timezone.utc).isoformat(),
                "series_id": series_id,
                "series_ep": ep_num,
            }
            save_episode(entry)
            update_job(job_id, status="done", progress=f"Episode {ep_num} complete",
                       result={"episode": entry, "sources": sources})

            series_list = load_series()
            s = next((x for x in series_list if x["id"] == series_id), None)
            if s:
                s["completed"] = s.get("completed", 0) + 1
                save_series(series_list)

        except Exception as e:
            import traceback; traceback.print_exc()
            update_job(job_id, status="error", error=str(e))

# ---------------------------------------------------------------------------
# CHAT
# ---------------------------------------------------------------------------

def chat_to_topic(user_message, existing_topics=None):
    context = ""
    if existing_topics:
        context = "EXISTING TOPICS FOR REFERENCE:\n"
        for t in existing_topics:
            context += f"#{t['rank']}: {t['title']}\n"
        context += "\n"

    system = """You are an editorial producer for an executive intelligence podcast.
Return a SINGLE topic object as valid JSON (no markdown):
{
  "rank": 1,
  "title": "provocative but professional title",
  "tension": "core contrarian thesis in 1-2 sentences",
  "why_it_matters": "strategic importance in 1 sentence",
  "common_mistake": "what sophisticated leaders get wrong",
  "sub_questions": ["question 1", "question 2", "question 3"],
  "trailer_hook": "3-4 sentence spoken-word hook",
  "production_brief": "2-3 sentences of specific guidance for the script writer"
}
Tone: sharp, executive, zero fluff."""

    data = call_anthropic([{
        "role": "user",
        "content": system + "\n\n" + context + user_message
    }], max_tokens=1000)

    text = extract_text(data).strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"): text = text[4:]
    return json.loads(text)

# ---------------------------------------------------------------------------
# SCRIPT GENERATION
# ---------------------------------------------------------------------------

def generate_grounded_script(topic, depth="standard", production_brief=""):
    brief_section = f"\nPRODUCTION BRIEF:\n{production_brief}\n" if production_brief else ""

    seg_min = {"executive": 10, "standard": 16, "deep": 24}.get(depth, 16)

    prompt = f"""You are writing a premium executive intelligence podcast script. This is NOT generic business content.

HOST PROFILES (stay in character - they are colleagues who push each other):
- ALEX: Former VP Advanced Analytics at a Fortune 100 CPG. Evidence-first thinker. Speaks in data, patterns, and structural causes. Gets impatient with hand-waving. Comfortable saying "the numbers don't support that." Direct without being abrasive.
- MORGAN: Former strategy consultant turned operator. Thinks in decisions, consequences, and capital. Asks "who benefits from this belief?" Challenges comfortable consensus. Often the one who names the thing everyone is thinking but not saying.

They have genuine intellectual disagreements. At least once per episode, Morgan should push back on something Alex says (or vice versa) with a real counter - not just "yes, and." Their dynamic makes the listener lean forward.

LISTENER PROFILE:
C-suite executive in analytics, AI, or data. Has 20+ years of experience. Reads Stratechery and The Economist, not TechCrunch. Is skeptical of AI hype but knows it's real. Works at the intersection of enterprise technology and business strategy. Specific context: beverage-alcohol industry, enterprise CPG, or analytics-led organizations.

TOPIC: {topic['title']}
CORE TENSION: {topic['tension']}
WHAT LEADERS GET WRONG: {topic.get('common_mistake', '')}
KEY QUESTIONS: {'; '.join(topic.get('sub_questions', []))}
WHY IT MATTERS: {topic.get('why_it_matters', '')}
{brief_section}

CONTENT STANDARDS (every episode must hit all of these):
- Name at least 2 specific companies, executives, or real situations as examples (not "a major CPG brand")
- Include at least one piece of specific data, a statistic, or a concrete number
- At least one moment where the hosts genuinely disagree and argue it out before resolving
- The close must leave the listener with ONE specific question to ask in their next leadership meeting
- Vary sentence rhythm - mix short punchy statements with longer analytical ones
- No filler phrases: "at the end of the day", "it's important to note", "in today's landscape", "let's dive in"
- Spoken-word only - no bullet points, no headers, no lists. Pure dialogue.

STRUCTURE (each section gets {max(2, seg_min//6)}-{max(3, seg_min//4)} segments):
1. COLD OPEN - Drop into the tension immediately. No throat-clearing. State something that makes the listener stop what they're doing.
2. GROUND IT - Concrete, named examples of what is actually happening right now. Specific companies, specific decisions, specific outcomes.
3. THE MECHANISM - Not what is happening, but WHY. The structural force, the incentive misalignment, the thing that makes this pattern repeat.
4. THE REAL MISTAKE - The specific error that smart, experienced leaders make. The more counterintuitive the better.
5. THE LEVER - What changes outcomes. One or two specific moves, not a framework. What would you actually do differently Monday morning?
6. THE REFRAME - Close with one idea that permanently changes how they see this topic. Not a summary. A new lens.

MINIMUM: {seg_min} segments total. Count before returning. Add more if under.
Each segment: 3-5 substantial spoken sentences. No one-liners.

Return ONLY a valid JSON array, no markdown, no preamble:
[{{"host": "Alex", "text": "..."}}, {{"host": "Morgan", "text": "..."}}, ...]

After the JSON array, on a new line:
SOURCES: source1, source2, source3"""

    try:
        try:
            data = call_anthropic([{"role": "user", "content": prompt}],
                                  max_tokens=4000, use_web_search=True)
            print("[SCRIPT] Web search enabled")
        except Exception as ws_err:
            print(f"[SCRIPT] Web search failed ({ws_err}), falling back to no-search")
            data = call_anthropic([{"role": "user", "content": prompt}],
                                  max_tokens=4000, use_web_search=False)
        full_text = extract_text(data)

        sources = []
        script_text = full_text
        if "SOURCES:" in full_text:
            parts = full_text.rsplit("SOURCES:", 1)
            script_text = parts[0].strip()
            sources = [s.strip() for s in parts[1].strip().split(",") if s.strip()]

        if "```" in script_text:
            script_text = script_text.split("```")[1]
            if script_text.startswith("json"): script_text = script_text[4:]

        script = json.loads(script_text.strip())
        print(f"[SCRIPT OK] {len(script)} segments generated")
        return script, sources
    except Exception as e:
        import traceback
        print(f"[SCRIPT FAIL] {type(e).__name__}: {e}")
        traceback.print_exc()
        sq = topic.get('sub_questions', [])
        title = topic['title']
        tension = topic['tension']
        matters = topic.get('why_it_matters', 'This has direct implications for how you allocate capital and talent.')
        mistake = topic.get('common_mistake', 'They optimize for visibility over actual impact, which means the real exposure never gets addressed.')
        return [
            {"host": "Alex", "text": f"Let's start with something most executives in this space already know but haven't fully acted on. {title}. The question isn't whether this is real - it's whether you're positioned correctly when it hits your organization."},
            {"host": "Morgan", "text": f"And the core tension is this: {tension} That's the uncomfortable part. Because it means the conventional playbook - the one that got most leaders to where they are - may actually be the wrong tool for what's coming."},
            {"host": "Alex", "text": f"Here's why this matters at the strategic level right now. {matters} And the window to get ahead of this is shorter than most leadership teams have internalized."},
            {"host": "Morgan", "text": "Let's ground this in what's actually happening. The organizations that are navigating this well aren't the ones with the biggest budgets or the most sophisticated tech stacks. They're the ones that identified the structural cause early and built around it rather than against it."},
            {"host": "Alex", "text": "The structural cause is key. Most conversations about this topic focus on symptoms - the visible friction, the metrics that are off, the talent gaps. But the mechanism underneath is an incentive misalignment that organizations keep papering over with process instead of fixing at the root."},
            {"host": "Morgan", "text": f"Which brings us to what sophisticated leaders consistently get wrong. {mistake} And the irony is that the leaders who are most experienced - who've solved hard problems before - are often the most prone to this mistake because their pattern recognition is calibrated to a different era."},
            {"host": "Alex", "text": f"The first question worth sitting with: {sq[0] if sq else 'Where in your current approach are you optimizing for the appearance of progress rather than the underlying condition?'} That's not a rhetorical question. It has a specific answer in your organization right now."},
            {"host": "Morgan", "text": f"And the second: {sq[1] if len(sq) > 1 else 'What would you do differently if you knew your current approach had a 24-month shelf life?'} Because the executives who are three moves ahead on this aren't smarter - they just asked that question earlier."},
            {"host": "Alex", "text": f"If there's a third lever worth examining: {sq[2] if len(sq) > 2 else 'How are you measuring whether your governance and your actual exposure are in sync?'} The answer tells you more about your real risk posture than any framework document."},
            {"host": "Morgan", "text": "Here's the practical implication. The next time this comes up - whether it's a board review, a budget cycle, or a talent discussion - the question isn't 'are we doing enough.' The question is 'are we working on the right thing.' Those are very different questions with very different answers."},
            {"host": "Alex", "text": "The executives who navigate this well aren't the ones with the best data or the biggest teams. They're the ones who identified where their mental model was wrong and updated it before the market forced them to. That's the actual competitive advantage here."},
            {"host": "Morgan", "text": f"Leave you with this reframe: {title} isn't a problem to solve. It's a condition to position around. The organizations that treat it as solvable will spend the next three years in reactive mode. The ones that treat it as structural reality will spend that same time building asymmetric advantage. That's the briefing."}
        ], []

def build_trailer_script(topic):
    return [
        {"host": "Alex", "text": topic.get("trailer_hook", topic["tension"])},
        {"host": "Alex", "text": f"For the full briefing on {topic['title']}, hit Generate Briefing. I'm Alex."}
    ], []

# ---------------------------------------------------------------------------
# RSS FEED
# ---------------------------------------------------------------------------

def build_feed():
    eps = load_episodes()
    feed_eps = sorted(
        [e for e in eps if not e.get("is_trailer")],
        key=lambda e: e.get("published", ""),
        reverse=True
    )

    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    items = []
    for ep in feed_eps:
        audio_url = f"{BASE_URL}/episodes/{esc(ep['file'])}"
        file_size = ep.get("file_size", 0)
        try:
            dt = datetime.fromisoformat(ep.get("published", ""))
            pub_rfc = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except Exception:
            pub_rfc = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

        items.append(f"""    <item>
      <title>{esc(ep.get('title', 'Intelligence Briefing'))}</title>
      <description>{esc(ep.get('description', ''))}</description>
      <enclosure url="{audio_url}" length="{file_size}" type="audio/mpeg"/>
      <guid isPermaLink="false">{esc(ep.get('id', audio_url))}</guid>
      <pubDate>{pub_rfc}</pubDate>
      <itunes:title>{esc(ep.get('title', 'Intelligence Briefing'))}</itunes:title>
      <itunes:summary>{esc(ep.get('description', ''))}</itunes:summary>
      <itunes:explicit>no</itunes:explicit>
    </item>""")

    feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
  xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Intelligence Briefings</title>
    <link>{BASE_URL}</link>
    <description>Executive intelligence briefings on AI, analytics, and enterprise strategy.</description>
    <language>en-us</language>
    <itunes:author>Ed Borasky</itunes:author>
    <itunes:owner><itunes:name>Ed Borasky</itunes:name></itunes:owner>
    <itunes:category text="Business"/>
    <itunes:explicit>no</itunes:explicit>
    <itunes:type>episodic</itunes:type>
    <lastBuildDate>{datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")}</lastBuildDate>
{chr(10).join(items)}
  </channel>
</rss>"""

    FEED_FILE.write_text(feed_xml, encoding="utf-8")
    print(f"[FEED] Rebuilt: {len(feed_eps)} episodes")

# ---------------------------------------------------------------------------
# AUDIO
# ---------------------------------------------------------------------------

def get_elevenlabs_client():
    from elevenlabs import ElevenLabs
    return ElevenLabs(api_key=ELEVEN_API_KEY)

def resolve_voice(client, name_or_id):
    resp = client.voices.get_all()
    for v in resp.voices:
        if v.name.lower() == name_or_id.lower() or v.voice_id == name_or_id:
            return v.voice_id
    for v in resp.voices:
        if name_or_id.lower() in v.name.lower():
            return v.voice_id
    return None

def generate_audio_bytes(client, text, voice_id):
    return b"".join(client.text_to_speech.convert(
        voice_id=voice_id, text=text,
        model_id="eleven_turbo_v2_5", output_format="mp3_44100_128"))

def generate_episode_audio(client, script, ep_dir, voice_a_id, voice_b_id):
    ep_dir = Path(ep_dir)
    ep_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    for i, seg in enumerate(script):
        vid = voice_a_id if seg["host"].lower() == "alex" else voice_b_id
        p = ep_dir / f"seg_{i:02d}.mp3"
        p.write_bytes(generate_audio_bytes(client, seg["text"], vid))
        parts.append(p)
    final = ep_dir / "episode.mp3"
    with open(final, "wb") as out:
        for f in parts:
            out.write(f.read_bytes())
    return final

# ---------------------------------------------------------------------------
# EPISODES
# ---------------------------------------------------------------------------

def load_episodes():
    if not EPISODES_JSON.exists(): return []
    try: return json.loads(EPISODES_JSON.read_text())
    except: return []

def save_episode(entry):
    eps = load_episodes()
    eps.append(entry)
    EPISODES_JSON.write_text(json.dumps(eps, indent=2))
    try:
        build_feed()
    except Exception as e:
        print(f"Feed build failed (non-fatal): {e}")

# ---------------------------------------------------------------------------
# BACKGROUND WORKERS
# ---------------------------------------------------------------------------

def _run_generate(job_id, topic_data, depth, voice_alex, voice_morgan, is_trailer, production_brief):
    try:
        update_job(job_id, status="running", progress="Connecting to voice service...")
        client = get_elevenlabs_client()
        voice_a_id = resolve_voice(client, voice_alex)
        voice_b_id = resolve_voice(client, voice_morgan)
        if not voice_a_id or not voice_b_id:
            update_job(job_id, status="error", error="Voice not found"); return

        if is_trailer:
            update_job(job_id, progress="Building trailer...")
            script, sources = build_trailer_script(topic_data)
        else:
            update_job(job_id, progress="Writing script - 1-2 minutes...")
            script, sources = generate_grounded_script(topic_data, depth, production_brief)
            log_production()

        update_job(job_id, progress=f"Generating audio ({len(script)} segments)...")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        ep_type = "trailer" if is_trailer else "episode"
        ep_id = f"briefing-{ep_type}-{timestamp}"
        ep_dir = EPISODES_DIR / ep_id
        final_path = generate_episode_audio(client, script, ep_dir, voice_a_id, voice_b_id)
        dest = EPISODES_DIR / f"{ep_id}.mp3"
        shutil.copy2(final_path, dest)

        label = "Trailer" if is_trailer else depth.title()
        entry = {
            "id": ep_id,
            "title": f"{'[Trailer] ' if is_trailer else ''}Briefing: {topic_data['title']}",
            "description": topic_data.get("tension", ""),
            "file": f"{ep_id}.mp3",
            "file_size": dest.stat().st_size,
            "depth": label,
            "is_trailer": is_trailer,
            "sources": sources,
            "published": datetime.now(timezone.utc).isoformat(),
        }
        save_episode(entry)
        update_job(job_id, status="done", progress="Complete",
                   result={"episode": entry, "sources": sources})

    except Exception as e:
        import traceback; traceback.print_exc()
        update_job(job_id, status="error", error=str(e))


def _run_chat(job_id, message, existing_topics, voice_alex, voice_morgan):
    try:
        update_job(job_id, status="running", progress="Generating topic from your message...")
        topic = chat_to_topic(message, existing_topics)
        production_brief = topic.get("production_brief", "")

        update_job(job_id, progress="Writing script - 1-2 minutes...")
        script, sources = generate_grounded_script(topic, depth="standard",
                                                    production_brief=production_brief)

        update_job(job_id, progress=f"Generating audio ({len(script)} segments)...")
        client = get_elevenlabs_client()
        voice_a_id = resolve_voice(client, voice_alex)
        voice_b_id = resolve_voice(client, voice_morgan)
        if not voice_a_id or not voice_b_id:
            update_job(job_id, status="error", error="Voice not found"); return

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        ep_id = f"briefing-chat-{timestamp}"
        ep_dir = EPISODES_DIR / ep_id
        final_path = generate_episode_audio(client, script, ep_dir, voice_a_id, voice_b_id)
        dest = EPISODES_DIR / f"{ep_id}.mp3"
        shutil.copy2(final_path, dest)
        log_production()

        entry = {
            "id": ep_id,
            "title": f"[Chat] {topic['title']}",
            "description": topic["tension"],
            "file": f"{ep_id}.mp3",
            "file_size": dest.stat().st_size,
            "depth": "Standard",
            "is_trailer": False,
            "sources": sources,
            "published": datetime.now(timezone.utc).isoformat(),
        }
        save_episode(entry)
        update_job(job_id, status="done", progress="Complete",
                   result={"topic": topic, "episode": entry, "sources": sources})

    except Exception as e:
        import traceback; traceback.print_exc()
        update_job(job_id, status="error", error=str(e))


def _run_create_series(series_id, topic_or_prompt, num_episodes, voice_alex, voice_morgan):
    try:
        series_list = load_series()
        s = next((x for x in series_list if x["id"] == series_id), None)
        if not s: return

        s["status"] = "outlining"
        save_series(series_list)
        episodes = generate_series_outline(topic_or_prompt, num_episodes)

        job_ids = []
        for ep in episodes:
            jid = create_job("series_ep", series_id=series_id, series_ep=ep["episode_number"])
            job_ids.append(jid)

        series_list = load_series()
        s = next((x for x in series_list if x["id"] == series_id), None)
        s["episodes"] = episodes
        s["job_ids"] = job_ids
        s["status"] = "producing"
        s["total"] = len(episodes)
        s["completed"] = 0
        save_series(series_list)

        _run_series(series_id, episodes, voice_alex, voice_morgan)

        series_list = load_series()
        s = next((x for x in series_list if x["id"] == series_id), None)
        if s:
            s["status"] = "done"
            save_series(series_list)

    except Exception as e:
        import traceback; traceback.print_exc()
        series_list = load_series()
        s = next((x for x in series_list if x["id"] == series_id), None)
        if s:
            s["status"] = "error"
            s["error"] = str(e)
            save_series(series_list)

# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------

@app.route('/api/engagement')
def api_engagement_summary():
    return jsonify(get_engagement_summary())

@app.route('/api/engagement', methods=['POST'])
def api_engagement_log():
    data = request.json or {}
    event_type = data.get("event_type", "")
    topic_title = data.get("topic_title", "")
    if not event_type or not topic_title:
        return jsonify({"success": False, "error": "event_type and topic_title required"}), 400
    save_engagement_event(
        event_type=event_type,
        topic_title=topic_title,
        episode_id=data.get("episode_id"),
        pct=data.get("pct"),
    )
    return jsonify({"success": True})


@app.route('/api/episodes/<ep_id>', methods=['DELETE'])
def api_episode_delete(ep_id):
    eps = load_episodes()
    target = next((e for e in eps if e.get("id") == ep_id), None)
    if not target:
        return jsonify({"success": False, "error": "Episode not found"}), 404

    eps = [e for e in eps if e.get("id") != ep_id]
    EPISODES_JSON.write_text(json.dumps(eps, indent=2))

    mp3_path = EPISODES_DIR / target.get("file", "")
    if mp3_path.exists():
        try:
            mp3_path.unlink()
        except Exception as e:
            print(f"[DELETE] Could not remove file {mp3_path}: {e}")

    seg_dir = EPISODES_DIR / ep_id
    if seg_dir.is_dir():
        try:
            shutil.rmtree(seg_dir)
        except Exception as e:
            print(f"[DELETE] Could not remove dir {seg_dir}: {e}")

    try:
        build_feed()
    except Exception as e:
        print(f"[DELETE] Feed rebuild failed (non-fatal): {e}")

    print(f"[DELETE] Removed episode {ep_id}: {target.get('title', '')}")
    return jsonify({"success": True, "deleted": ep_id, "title": target.get("title", "")})


@app.route('/api/test/web-search')
def api_test_web_search():
    test_prompt = "What is today's date? Answer in one sentence."
    try:
        data = call_anthropic(
            [{"role": "user", "content": test_prompt}],
            max_tokens=100,
            use_web_search=True
        )
        text = extract_text(data)
        tool_uses = [b for b in data.get("content", []) if b.get("type") == "tool_use"]
        search_used = any(b.get("name") == "web_search" for b in tool_uses)
        return jsonify({
            "success": True,
            "web_search_invoked": search_used,
            "response_preview": text[:200],
            "stop_reason": data.get("stop_reason"),
            "note": "Web search is working" if search_used else "Call succeeded but web search was not invoked"
        })
    except Exception as e:
        error_str = str(e)
        billing = any(kw in error_str.lower() for kw in ["credit", "quota", "billing", "unauthorized", "403", "permission"])
        return jsonify({
            "success": False,
            "web_search_invoked": False,
            "error": error_str,
            "likely_cause": "Billing/permissions issue" if billing else "API error",
        }), 500


@app.route('/api/health')
def api_health():
    result = {
        "elevenlabs": {"status": "unknown", "characters_remaining": None, "characters_limit": None, "warning": False},
        "anthropic": {"status": "unknown", "warning": False},
    }

    if not ELEVEN_API_KEY:
        result["elevenlabs"] = {"status": "missing_key", "warning": True}
    else:
        try:
            import urllib.request as ur
            req = ur.Request(
                "https://api.elevenlabs.io/v1/user/subscription",
                headers={"xi-api-key": ELEVEN_API_KEY}
            )
            with ur.urlopen(req, timeout=10) as resp:
                sub = json.loads(resp.read())
            used = sub.get("character_count", 0)
            limit = sub.get("character_limit", 0)
            remaining = limit - used
            pct_used = (used / limit * 100) if limit else 0
            warning = pct_used >= 80
            result["elevenlabs"] = {
                "status": "ok",
                "characters_used": used,
                "characters_limit": limit,
                "characters_remaining": remaining,
                "pct_used": round(pct_used, 1),
                "warning": warning,
                "tier": sub.get("tier", "unknown"),
            }
        except Exception as e:
            status_code = getattr(e, 'code', None)
            if status_code == 401:
                result["elevenlabs"] = {"status": "invalid_key", "warning": True}
            else:
                result["elevenlabs"] = {"status": "error", "error": str(e), "warning": False}

    if not ANTHROPIC_API_KEY:
        result["anthropic"] = {"status": "missing_key", "warning": True}
    else:
        try:
            jobs = get_all_jobs()
            recent_errors = [j.get("error", "") for j in jobs.values()
                             if j.get("status") == "error" and j.get("error")][-10:]
            billing_error = any(
                any(kw in err.lower() for kw in ["credit", "quota", "billing", "overload", "rate limit", "insufficient"])
                for err in recent_errors
            )
            result["anthropic"] = {
                "status": "ok",
                "key_configured": True,
                "warning": billing_error,
                "warning_reason": "Recent job errors suggest quota or billing issues" if billing_error else None,
            }
        except Exception as e:
            result["anthropic"] = {"status": "error", "error": str(e), "warning": False}

    overall_warning = result["elevenlabs"]["warning"] or result["anthropic"]["warning"]
    return jsonify({"health": result, "any_warning": overall_warning})


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/listen')
def listener():
    return render_template('listen.html')

@app.route('/episodes/<path:filename>')
def serve_episode(filename):
    return send_from_directory(EPISODES_DIR, filename)

@app.route('/feed.xml')
def serve_feed():
    try:
        build_feed()
    except Exception as e:
        print(f"Feed rebuild failed: {e}")
    if FEED_FILE.exists():
        return send_from_directory(DATA_DIR, 'feed.xml', mimetype='application/rss+xml')
    return "No episodes yet.", 404

# --- Admin ---

@app.route('/api/queue', methods=['GET'])
def api_queue_status():
    jobs = get_all_jobs()
    active = [j for j in jobs.values() if j["status"] in ("queued", "running")]
    active.sort(key=lambda j: j["created_at"])
    return jsonify({"active": active, "total_active": len(active)})

@app.route('/api/queue/clear', methods=['POST'])
def api_queue_clear():
    cleared = clear_queue()
    return jsonify({"success": True, "cleared": cleared})

@app.route('/api/feed/rebuild', methods=['POST'])
def api_feed_rebuild():
    try:
        build_feed()
        ep_count = len([e for e in load_episodes() if not e.get("is_trailer")])
        return jsonify({"success": True, "episodes_in_feed": ep_count})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/autoqueue', methods=['POST'])
def api_autoqueue():
    data = request.json or {}
    voice_alex = data.get("voice_alex", "Chris - Charming, Down-to-Earth")
    voice_morgan = data.get("voice_morgan", "Matilda - Knowledgable, Professional")
    if not ANTHROPIC_API_KEY or not ELEVEN_API_KEY:
        return jsonify({"success": False, "error": "API keys not configured"})
    job_id = autoqueue_ar_topic(voice_alex, voice_morgan)
    if job_id:
        return jsonify({"success": True, "job_id": job_id})
    return jsonify({"success": False, "error": "Auto-queue failed - check AR dashboard connectivity"})

@app.route('/api/discover/suggestions', methods=['GET'])
def api_discover_suggestions():
    SUGGESTIONS_CACHE = DATA_DIR / "suggestions_cache.json"
    today = date.today().isoformat()
    force = request.args.get("refresh", "").lower() == "true"

    if not force and SUGGESTIONS_CACHE.exists():
        try:
            cached = json.loads(SUGGESTIONS_CACHE.read_text())
            if cached.get("date") == today:
                topics_mtime = TOPICS_CACHE.stat().st_mtime if TOPICS_CACHE.exists() else 0
                suggestions_mtime = SUGGESTIONS_CACHE.stat().st_mtime
                if suggestions_mtime >= topics_mtime:
                    return jsonify({"suggestions": cached["suggestions"], "cached": True})
                else:
                    print("[SUGGESTIONS] Cache predates today's topics - regenerating")
        except Exception:
            pass

    today_topics = []
    if TOPICS_CACHE.exists():
        try:
            tc = json.loads(TOPICS_CACHE.read_text())
            if tc.get("date") == today:
                today_topics = [t["title"] for t in tc.get("topics", [])]
        except Exception:
            pass

    eps = load_episodes()
    commissioned = [e["title"] for e in eps if not e.get("is_trailer")][-30:]
    series_list = load_series()
    series_titles = [s["title"] for s in series_list]
    all_exclusions = list(set(today_topics + commissioned))

    exclusion_block = "\n".join(f"- {t}" for t in all_exclusions) if all_exclusions else "None yet."
    series_block = "\n".join(f"- {t}" for t in series_titles) if series_titles else "None yet."

    eng = get_engagement_summary()
    strong_block = "\n".join(f"- {t}" for t in eng.get("strong_interest", [])) or "None yet."
    moderate_block = "\n".join(f"- {t}" for t in eng.get("moderate_interest", [])) or "None yet."
    dismissed_block = "\n".join(f"- {t}" for t in eng.get("dismissed", [])) or "None yet."

    prompt = f"""You are an editorial intelligence engine for a senior analytics/AI executive (CAO at Overproof, former VP Analytics at Diageo North America).

Generate 10 high-signal topic ideas that are NEW and DISTINCT from everything already covered.

ALREADY COVERED - DO NOT GENERATE ANYTHING SIMILAR TO THESE:
{exclusion_block}

SERIES ARCS IN PROGRESS:
{series_block}

BEHAVIORAL SIGNALS - USE THESE TO CALIBRATE RECOMMENDATIONS:
Strong interest (listened 75%+ or completed): Generate topics that go DEEPER or adjacent to these.
{strong_block}

Moderate interest (previewed or partially listened): Good signal for adjacent angles.
{moderate_block}

Dismissed (explicitly not interested): AVOID topics in this territory.
{dismissed_block}

EXECUTIVE PROFILE:
- Thinks in systems, incentives, capital allocation, and moats
- Enterprise AI: governance, multi-agent systems, ROI measurement
- Beverage alcohol: three-tier dynamics, venue intelligence, distributor data
- Org design: CAO/CDO role evolution, analytics talent strategy
- Active C-suite job seeker: CAO, CDO, VP Analytics

RULES:
- Every topic must be meaningfully distinct from the exclusion list
- Weight toward strong_interest adjacencies - these are proven signals
- Do NOT generate anything in the dismissed territory
- Include 2-3 trending topics from the last 30 days
- Contrarian angle required - challenges comfortable consensus
- Zero generic AI hype topics

Return ONLY a valid JSON array of exactly 10 objects, no markdown, no preamble:
[{{
  "rank": 1,
  "title": "provocative but professional title",
  "tension": "core contrarian thesis in 1-2 sentences",
  "why_it_matters": "strategic importance for this specific executive in 1 sentence",
  "freshness": "evergreen | trending | time-sensitive",
  "confidence_rationale": "1 sentence: why this maps to your demonstrated interests"
}}]"""

    try:
        try:
            data = call_anthropic([{"role": "user", "content": prompt}], max_tokens=3000, use_web_search=True)
        except Exception as ws_err:
            print(f"[SUGGESTIONS] Web search failed ({ws_err}), falling back")
            data = call_anthropic([{"role": "user", "content": prompt}], max_tokens=3000, use_web_search=False)

        text = extract_text(data).strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"): text = text[4:]
        bracket = text.find("[")
        if bracket > 0:
            text = text[bracket:]
        suggestions = json.loads(text)[:10]
        SUGGESTIONS_CACHE.write_text(json.dumps({"date": today, "suggestions": suggestions}, indent=2))
        return jsonify({"suggestions": suggestions, "cached": False})
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[SUGGESTIONS] Failed: {e}")
        return jsonify({"suggestions": [], "error": str(e), "cached": False}), 500


@app.route('/api/cron/nightly-trailers', methods=['GET', 'POST'])
def api_cron_nightly_trailers():
    """
    Nightly job: generate 6 high-confidence trailer topics and queue them for production.
    Cron-job.org URL: /api/cron/nightly-trailers?secret=YOUR_CRON_SECRET
    Recommended: Daily at 22:00 America/Chicago
    """
    secret = request.args.get("secret") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if CRON_SECRET and secret != CRON_SECRET:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    force = request.args.get("force", "").lower() == "true"
    TRAILER_QUEUE_LOG = DATA_DIR / "nightly_trailer_log.json"
    today = date.today().isoformat()

    if not force and TRAILER_QUEUE_LOG.exists():
        try:
            log = json.loads(TRAILER_QUEUE_LOG.read_text())
            if log.get("date") == today:
                return jsonify({"success": True, "skipped": True,
                                "reason": "Already ran today", "job_ids": log.get("job_ids", [])})
        except Exception:
            pass

    if not ANTHROPIC_API_KEY or not ELEVEN_API_KEY:
        return jsonify({"success": False, "error": "API keys not configured"}), 500

    today_topics_list = []
    if TOPICS_CACHE.exists():
        try:
            tc = json.loads(TOPICS_CACHE.read_text())
            if tc.get("date") == today:
                today_topics_list = [t["title"] for t in tc.get("topics", [])]
        except Exception:
            pass

    eps = load_episodes()
    commissioned_titles = [e["title"] for e in eps if not e.get("is_trailer")][-20:]
    series_list = load_series()
    series_titles = [s["title"] for s in series_list]
    all_exclusions = list(set(today_topics_list + commissioned_titles))

    exclusion_block = "\n".join(f"- {t}" for t in all_exclusions) if all_exclusions else "None yet."
    series_block = "\n".join(f"- {t}" for t in series_titles) if series_titles else "None yet."

    eng = get_engagement_summary()
    strong_block = "\n".join(f"- {t}" for t in eng.get("strong_interest", [])) or "None yet."
    moderate_block = "\n".join(f"- {t}" for t in eng.get("moderate_interest", [])) or "None yet."
    dismissed_block = "\n".join(f"- {t}" for t in eng.get("dismissed", [])) or "None yet."

    prompt = f"""You are a high-precision editorial recommender for a senior analytics/AI executive (CAO at Overproof, former VP Analytics Diageo North America).

Your task: Generate exactly 6 topic candidates for overnight trailer production.

ALREADY COVERED - DO NOT GENERATE ANYTHING SIMILAR:
{exclusion_block}

SERIES ARCS IN PROGRESS:
{series_block}

BEHAVIORAL SIGNALS (actual listen data - weight these heavily):
Strong interest - listened 75%+ or completed:
{strong_block}

Moderate interest - previewed or partial listen:
{moderate_block}

Dismissed - explicitly rejected. Stay away:
{dismissed_block}

EXECUTIVE PROFILE:
- Ed Dobbles, CAO at Overproof (beverage alcohol AI/analytics)
- Former VP Advanced Analytics, Diageo North America
- Building multi-agent AI governance frameworks
- Enterprise deals with Heineken, Beam Suntory, Diageo
- Thinks in systems, incentives, moats, capital allocation

CONSTRAINTS:
- All 6 must clear a HIGH confidence bar
- Topics near strong_interest adjacencies get priority
- NEVER generate anything in the dismissed territory
- Mix: 2-3 enterprise AI/governance, 1-2 beverage alcohol/CPG, 1-2 org/talent/career
- At least 2 TIME-SENSITIVE topics (next 30 days)

Return ONLY a valid JSON array of exactly 6 objects, no markdown:
[{{
  "rank": 1,
  "title": "provocative but professional title",
  "tension": "core contrarian thesis in 1-2 sentences",
  "why_it_matters": "strategic importance in 1 sentence",
  "common_mistake": "what sophisticated leaders get wrong",
  "sub_questions": ["question 1", "question 2", "question 3"],
  "trailer_hook": "3-4 sentence spoken-word hook, direct to a peer executive",
  "confidence_score": 85,
  "confidence_rationale": "1-2 sentences: specific reason this is high-confidence"
}}]"""

    try:
        data = call_anthropic([{"role": "user", "content": prompt}], max_tokens=3000, use_web_search=True)
    except Exception:
        data = call_anthropic([{"role": "user", "content": prompt}], max_tokens=3000, use_web_search=False)

    text = extract_text(data).strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"): text = text[4:]
    trailer_topics = json.loads(text)[:6]

    job_ids = []
    voice_alex = "Chris - Charming, Down-to-Earth"
    voice_morgan = "Matilda - Knowledgable, Professional"

    for topic in trailer_topics:
        jid = create_job("generate")
        threading.Thread(
            target=_run_generate,
            args=(jid, topic, "executive", voice_alex, voice_morgan, True, ""),
            daemon=True
        ).start()
        job_ids.append({"job_id": jid, "title": topic["title"], "confidence": topic.get("confidence_score", 0)})

    TRAILER_QUEUE_LOG.write_text(json.dumps({
        "date": today,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "job_ids": job_ids,
        "topics": trailer_topics,
    }, indent=2))

    print(f"[NIGHTLY] Queued {len(job_ids)} trailers")
    return jsonify({"success": True, "queued": len(job_ids), "job_ids": job_ids,
                    "triggered_at": datetime.now(timezone.utc).isoformat()})


@app.route('/api/cron/nightly-trailers/status', methods=['GET'])
def api_nightly_trailer_status():
    TRAILER_QUEUE_LOG = DATA_DIR / "nightly_trailer_log.json"
    if not TRAILER_QUEUE_LOG.exists():
        return jsonify({"run": None})
    try:
        log = json.loads(TRAILER_QUEUE_LOG.read_text())
        for item in log.get("job_ids", []):
            j = get_job(item["job_id"])
            item["status"] = j["status"] if j else "unknown"
        return jsonify({"run": log})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/cron/autoqueue', methods=['GET', 'POST'])
def api_cron_autoqueue():
    """
    Scheduled autoqueue endpoint - called by external cron (cron-job.org or similar).
    Recommended schedule: Daily at 06:00 America/Chicago
    URL: /api/cron/autoqueue?secret=YOUR_CRON_SECRET
    """
    secret = request.args.get("secret") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if CRON_SECRET and secret != CRON_SECRET:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    if not ANTHROPIC_API_KEY or not ELEVEN_API_KEY:
        return jsonify({"success": False, "error": "API keys not configured"}), 500

    job_id = autoqueue_ar_topic(
        voice_alex="Chris - Charming, Down-to-Earth",
        voice_morgan="Matilda - Knowledgable, Professional"
    )
    if job_id:
        print(f"[CRON] Auto-queued AR briefing -> job {job_id}")
        return jsonify({"success": True, "job_id": job_id, "triggered_at": datetime.now(timezone.utc).isoformat()})
    return jsonify({"success": False, "error": "Autoqueue failed"}), 500


# ---------------------------------------------------------------------------
# MORNING PREP CRON - Pre-warms topics and suggestions cache before you open the page
# ---------------------------------------------------------------------------

@app.route('/api/cron/morning-prep', methods=['GET', 'POST'])
def api_cron_morning_prep():
    """
    Morning warm-up job: pre-build topics cache and suggestions cache
    so the page loads instantly when opened.
    Recommended: Daily at 05:30 America/Chicago (11:30 UTC)
    URL: /api/cron/morning-prep?secret=YOUR_CRON_SECRET
    """
    secret = request.args.get("secret") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if CRON_SECRET and secret != CRON_SECRET:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    force = request.args.get("force", "").lower() == "true"
    MORNING_PREP_LOG = DATA_DIR / "morning_prep_log.json"
    today = date.today().isoformat()

    # Idempotency: don't run twice in one day
    if not force and MORNING_PREP_LOG.exists():
        try:
            log = json.loads(MORNING_PREP_LOG.read_text())
            if log.get("date") == today:
                return jsonify({"success": True, "skipped": True,
                                "reason": "Already ran today", "cached_at": log.get("run_at")})
        except Exception:
            pass

    results = {"topics": False, "suggestions": False, "errors": []}

    # Step 1: Pre-warm topics cache
    try:
        topics = get_topics_for_today()
        results["topics"] = len(topics)
        print(f"[MORNING PREP] Topics warmed: {len(topics)} topics")
    except Exception as e:
        results["errors"].append(f"Topics failed: {str(e)}")
        print(f"[MORNING PREP] Topics failed: {e}")

    # Step 2: Pre-warm suggestions cache
    SUGGESTIONS_CACHE = DATA_DIR / "suggestions_cache.json"
    try:
        today_topics = []
        if TOPICS_CACHE.exists():
            try:
                tc = json.loads(TOPICS_CACHE.read_text())
                if tc.get("date") == today:
                    today_topics = [t["title"] for t in tc.get("topics", [])]
            except Exception:
                pass

        eps = load_episodes()
        commissioned = [e["title"] for e in eps if not e.get("is_trailer")][-30:]
        series_list = load_series()
        series_titles = [s["title"] for s in series_list]
        all_exclusions = list(set(today_topics + commissioned))

        exclusion_block = "\n".join(f"- {t}" for t in all_exclusions) if all_exclusions else "None yet."
        series_block = "\n".join(f"- {t}" for t in series_titles) if series_titles else "None yet."
        eng = get_engagement_summary()
        strong_block = "\n".join(f"- {t}" for t in eng.get("strong_interest", [])) or "None yet."
        moderate_block = "\n".join(f"- {t}" for t in eng.get("moderate_interest", [])) or "None yet."
        dismissed_block = "\n".join(f"- {t}" for t in eng.get("dismissed", [])) or "None yet."

        prompt = f"""You are an editorial intelligence engine for a senior analytics/AI executive (CAO at Overproof, former VP Analytics at Diageo North America).

Generate 10 high-signal topic ideas that are NEW and DISTINCT from everything already covered.

ALREADY COVERED - DO NOT GENERATE ANYTHING SIMILAR TO THESE:
{exclusion_block}

SERIES ARCS IN PROGRESS:
{series_block}

BEHAVIORAL SIGNALS - USE THESE TO CALIBRATE RECOMMENDATIONS:
Strong interest (listened 75%+ or completed): Generate topics that go DEEPER or adjacent to these.
{strong_block}

Moderate interest (previewed or partially listened): Good signal for adjacent angles.
{moderate_block}

Dismissed (explicitly not interested): AVOID topics in this territory.
{dismissed_block}

EXECUTIVE PROFILE:
- Thinks in systems, incentives, capital allocation, and moats
- Enterprise AI: governance, multi-agent systems, ROI measurement
- Beverage alcohol: three-tier dynamics, venue intelligence, distributor data
- Org design: CAO/CDO role evolution, analytics talent strategy
- Active C-suite job seeker: CAO, CDO, VP Analytics

RULES:
- Every topic must be meaningfully distinct from the exclusion list
- Weight toward strong_interest adjacencies - these are proven signals
- Do NOT generate anything in the dismissed territory
- Include 2-3 trending topics from the last 30 days
- Contrarian angle required - challenges comfortable consensus
- Zero generic AI hype topics

Return ONLY a valid JSON array of exactly 10 objects, no markdown, no preamble:
[{{
  "rank": 1,
  "title": "provocative but professional title",
  "tension": "core contrarian thesis in 1-2 sentences",
  "why_it_matters": "strategic importance for this specific executive in 1 sentence",
  "freshness": "evergreen | trending | time-sensitive",
  "confidence_rationale": "1 sentence: why this maps to your demonstrated interests"
}}]"""

        try:
            data = call_anthropic([{"role": "user", "content": prompt}], max_tokens=3000, use_web_search=True)
        except Exception:
            data = call_anthropic([{"role": "user", "content": prompt}], max_tokens=3000, use_web_search=False)

        text = extract_text(data).strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"): text = text[4:]
        bracket = text.find("[")
        if bracket > 0:
            text = text[bracket:]
        suggestions = json.loads(text)[:10]
        SUGGESTIONS_CACHE.write_text(json.dumps({"date": today, "suggestions": suggestions}, indent=2))
        results["suggestions"] = len(suggestions)
        print(f"[MORNING PREP] Suggestions warmed: {len(suggestions)} topics")
    except Exception as e:
        results["errors"].append(f"Suggestions failed: {str(e)}")
        print(f"[MORNING PREP] Suggestions failed: {e}")

    # Log run
    MORNING_PREP_LOG.write_text(json.dumps({
        "date": today,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }, indent=2))

    success = results["topics"] is not False
    print(f"[MORNING PREP] Complete - topics: {results['topics']}, suggestions: {results['suggestions']}")
    return jsonify({
        "success": success,
        "date": today,
        "topics_cached": results["topics"],
        "suggestions_cached": results["suggestions"],
        "errors": results["errors"],
        "run_at": datetime.now(timezone.utc).isoformat(),
    })


# --- Series ---

@app.route('/api/series', methods=['GET'])
def api_series_list():
    series = load_series()
    return jsonify({"series": series})

@app.route('/api/series', methods=['POST'])
def api_series_create():
    data = request.json or {}
    topic_data = data.get("topic_data")
    topic_str = data.get("topic", "").strip()
    num_episodes = int(data.get("num_episodes", 6))
    voice_alex = data.get("voice_alex", "Chris - Charming, Down-to-Earth")
    voice_morgan = data.get("voice_morgan", "Matilda - Knowledgable, Professional")

    if not topic_data and not topic_str:
        return jsonify({"success": False, "error": "topic or topic_data required"})
    if not ANTHROPIC_API_KEY or not ELEVEN_API_KEY:
        return jsonify({"success": False, "error": "API keys not configured"})

    topic_or_prompt = topic_data if topic_data else topic_str
    series_title = topic_data["title"] if topic_data else topic_str[:80]

    series_id = str(uuid.uuid4())[:8]
    series_entry = {
        "id": series_id,
        "title": series_title,
        "num_episodes": num_episodes,
        "status": "queued",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "episodes": [],
        "job_ids": [],
        "total": num_episodes,
        "completed": 0,
        "error": None,
    }
    series_list = load_series()
    series_list.append(series_entry)
    save_series(series_list)

    threading.Thread(
        target=_run_create_series,
        args=(series_id, topic_or_prompt, num_episodes, voice_alex, voice_morgan),
        daemon=True
    ).start()

    return jsonify({"success": True, "series_id": series_id, "title": series_title})

@app.route('/api/series/<series_id>', methods=['GET'])
def api_series_status(series_id):
    series = load_series()
    s = next((x for x in series if x["id"] == series_id), None)
    if not s:
        return jsonify({"error": "Series not found"}), 404
    if s.get("job_ids"):
        job_statuses = []
        for i, jid in enumerate(s["job_ids"]):
            j = get_job(jid)
            ep_title = s["episodes"][i]["title"] if i < len(s.get("episodes", [])) else f"Episode {i+1}"
            job_statuses.append({
                "episode": i + 1,
                "title": ep_title,
                "job_id": jid,
                "status": j["status"] if j else "unknown",
                "progress": j.get("progress", "") if j else "",
            })
        s["job_statuses"] = job_statuses
    return jsonify(s)

# --- Core ---

@app.route('/api/job/<job_id>')
def api_job_status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)

@app.route('/api/voices')
def api_voices():
    if not ELEVEN_API_KEY:
        return jsonify({"error": "No API key", "voices": []})
    try:
        client = get_elevenlabs_client()
        resp = client.voices.get_all()
        voices = [{"name": v.name, "voice_id": v.voice_id, "category": v.category or "custom"}
                  for v in resp.voices]
        return jsonify({"voices": voices})
    except Exception as e:
        return jsonify({"error": str(e), "voices": []})

@app.route('/api/episodes')
def api_episodes():
    eps = load_episodes()
    trailers = [e for e in eps if e.get("is_trailer")]
    full = [e for e in eps if not e.get("is_trailer")]
    return jsonify({"episodes": eps, "full_count": len(full), "trailer_count": len(trailers)})

@app.route('/api/topics')
def api_topics():
    try:
        topics = get_topics_for_today()
        week_count = productions_this_week()
        return jsonify({"topics": topics, "productions_this_week": week_count, "weekly_cap": WEEKLY_CAP})
    except Exception as e:
        return jsonify({"error": str(e), "topics": FALLBACK_TOPICS,
                        "productions_this_week": 0, "weekly_cap": WEEKLY_CAP})

@app.route('/api/chat', methods=['POST'])
def api_chat():
    data = request.json
    message = data.get("message", "").strip()
    existing_topics = data.get("existing_topics", [])
    voice_alex = data.get("voice_alex", "Chris - Charming, Down-to-Earth")
    voice_morgan = data.get("voice_morgan", "Matilda - Knowledgable, Professional")

    if not message:
        return jsonify({"success": False, "error": "Message required"})
    if not ANTHROPIC_API_KEY:
        return jsonify({"success": False, "error": "Anthropic API key not configured"})
    if not ELEVEN_API_KEY:
        return jsonify({"success": False, "error": "ElevenLabs API key not configured"})

    job_id = create_job("chat")
    threading.Thread(
        target=_run_chat,
        args=(job_id, message, existing_topics, voice_alex, voice_morgan),
        daemon=True
    ).start()
    return jsonify({"success": True, "job_id": job_id, "status": "queued"})

@app.route('/api/generate', methods=['POST'])
def api_generate():
    data = request.json
    topic_data = data.get("topic_data")
    topic_title = data.get("topic", "").strip()
    depth = data.get("depth", "standard")
    voice_alex = data.get("voice_alex", "Chris - Charming, Down-to-Earth")
    voice_morgan = data.get("voice_morgan", "Matilda - Knowledgable, Professional")
    is_trailer = data.get("trailer", False)
    production_brief = data.get("production_brief", "")

    if not topic_data and not topic_title:
        return jsonify({"success": False, "error": "Topic required"})
    if not ELEVEN_API_KEY:
        return jsonify({"success": False, "error": "ElevenLabs API key not configured"})

    if not topic_data:
        topic_data = {"title": topic_title, "tension": topic_title,
                      "why_it_matters": "", "common_mistake": "", "sub_questions": [],
                      "trailer_hook": data.get("trailer_hook", topic_title)}

    job_id = create_job("generate")
    threading.Thread(
        target=_run_generate,
        args=(job_id, topic_data, depth, voice_alex, voice_morgan, is_trailer, production_brief),
        daemon=True
    ).start()
    return jsonify({"success": True, "job_id": job_id, "status": "queued"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)