"""Facts Nobody Told You - fully automatic Reel maker + poster.
Usage:  python main.py make   -> writes the video into ./videos
        python main.py post   -> publishes it to Instagram
"""
import asyncio, base64, json, math, os, pathlib, random, subprocess, sys, time
import requests

ROOT = pathlib.Path(__file__).parent
VIDEOS, TMP = ROOT / "videos", ROOT / "tmp"
HISTORY, META = ROOT / "history.json", TMP / "meta.json"

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
PIXABAY_KEY = os.environ.get("PIXABAY_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
VOICE = os.environ.get("VOICE", "en-US-AndrewNeural")
GV = os.environ.get("GRAPH_VERSION", "v23.0")
DRY = os.environ.get("DRY_RUN", "0") == "1"

CATEGORIES = ["space", "the human body", "world history", "psychology", "animals",
              "the ocean", "geography", "technology", "science", "money and economics",
              "food science", "weather and the earth", "ancient civilizations", "the brain"]


def run(cmd, **kw):
    subprocess.run(cmd, check=True, **kw)


# ---------- 1. script (Gemini) ----------
def make_script():
    hist = json.loads(HISTORY.read_text()) if HISTORY.exists() else []
    cat = random.choice(CATEGORIES)
    prompt = f"""You write 30-40 second narrated Instagram Reels for a facts page called "Facts Nobody Told You".
Category today: {cat}.
  Pick ONE fresh, surprising topic. Do NOT repeat or resemble these past topics: {hist[-80:]}
Rules: use only well-established, verifiable facts. No exaggeration, no invented numbers, no myths presented as fact.
Return JSON with exactly these keys:
topic (short string), hook (max 10 words, curiosity-driven opening line),
facts (list of 3 or 4 sentences, each under 25 words),
outro (one short line asking viewers to follow or save),
caption (1-2 sentences ending with a question),
hashtags (list of 5 without the # sign),
search_terms (list of 4 simple English words for stock footage that fits the topic)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    r = requests.post(url, params={"key": GEMINI_KEY}, timeout=600, json={
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0}})
    if not r.ok:
        sys.exit(f"Gemini error {r.status_code}: {r.text[:300]}")
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


# ---------- 2. voice + word timings (edge-tts) ----------
async def tts(text, path):
    import edge_tts
    com = edge_tts.Communicate(text, VOICE, boundary="WordBoundary")
    words = []
    with open(path, "wb") as f:
        async for ch in com.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                s = ch["offset"] / 1e7
                words.append((ch["text"], s, s + ch["duration"] / 1e7))
    if not words:
        sys.exit("Voice generation returned no words")
    return words


# ---------- 3. captions (ASS file) ----------
def ts(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def make_ass(words, path, per=3):
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n"
            "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
            "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,"
            "Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
            "Style: Default,DejaVu Sans,96,&H0000D7FF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,2,5,60,60,0,1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
    chunks = [words[i:i + per] for i in range(0, len(words), per)]
    lines = []
    for i, ch in enumerate(chunks):
        start = ch[0][1]
        end = chunks[i + 1][0][1] if i + 1 < len(chunks) else ch[-1][2] + 0.4
        txt = " ".join(w[0] for w in ch).upper().replace("{", "").replace("}", "")
        lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Default,,0,0,0,,{txt}")
    pathlib.Path(path).write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


# ---------- 4. stock footage (Pixabay) ----------
def fetch_clips(terms, need):
    TMP.mkdir(exist_ok=True)
    got, seen = [], set()
    for q in terms * 2:
        if len(got) >= need:
            break
        r = requests.get("https://pixabay.com/api/videos/", timeout=30,
                         params={"key": PIXABAY_KEY, "q": q, "per_page": 20, "safesearch": "true"})
        if not r.ok:
            sys.exit(f"Pixabay error {r.status_code}: {r.text[:200]}")
        hits = r.json().get("hits", [])
        random.shuffle(hits)
        for h in hits:
            v = h.get("videos", {})
            v = v.get("medium") or v.get("small") or v.get("large")
            if not v or not v.get("url") or h["id"] in seen:
                continue
            seen.add(h["id"])
            p = TMP / f"clip{len(got)}.mp4"
            with requests.get(v["url"], stream=True, timeout=60) as d:
                d.raise_for_status()
                with open(p, "wb") as f:
                    for b in d.iter_content(1 << 16):
                        f.write(b)
            got.append(p)
            break
    if not got:
        sys.exit("No stock clips found")
    return got


# ---------- 5. assemble video (ffmpeg) ----------
def build_video(clips, audio, ass_name, dur, out):
    n = max(4, math.ceil(dur / 5))
    seg = dur / n
    listing = []
    for i in range(n):
        src = clips[i % len(clips)]
        o = TMP / f"seg{i}.mp4"
        run(["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-i", str(src), "-t", f"{seg:.2f}",
             "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,setsar=1",
             "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p", str(o)])
        listing.append(f"file '{o.resolve()}'")
    (TMP / "list.txt").write_text("\n".join(listing))
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "list.txt",
         "-i", str(pathlib.Path(audio).resolve()),
         "-vf", f"eq=brightness=-0.10,ass={ass_name}", "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-t", f"{dur:.2f}", "-movflags", "+faststart",
         str(pathlib.Path(out).resolve())], cwd=TMP)


def make():
    TMP.mkdir(exist_ok=True)
    VIDEOS.mkdir(exist_ok=True)
    data = make_script()
    narration = " ".join([data["hook"]] + data["facts"] + [data["outro"]])
    print("TOPIC:", data["topic"], "\n", narration)
    words = asyncio.run(tts(narration, TMP / "voice.mp3"))
    make_ass(words, TMP / "captions.ass")
    dur = words[-1][2] + 0.8
    clips = fetch_clips(data["search_terms"], max(4, math.ceil(dur / 5)))
    name = time.strftime("%Y%m%d-%H%M%S") + ".mp4"
    build_video(clips, TMP / "voice.mp3", "captions.ass", dur, VIDEOS / name)
    tags = " ".join("#" + t.lstrip("#").replace(" ", "") for t in data["hashtags"])
    META.write_text(json.dumps({"file": name, "caption": f"{data['caption']}\n\n{tags}"}))
    if not DRY:
        hist = json.loads(HISTORY.read_text()) if HISTORY.exists() else []
        HISTORY.write_text(json.dumps((hist + [data["topic"]])[-300:], indent=1))
    print("Video ready:", name)


# ---------- 6. Instagram ----------
def update_secret(new_token):
    pat, repo = os.environ.get("GH_PAT"), os.environ.get("GITHUB_REPOSITORY")
    if not (pat and repo):
        return
    from nacl import encoding, public
    h = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"}
    k = requests.get(f"https://api.github.com/repos/{repo}/actions/secrets/public-key", headers=h, timeout=30).json()
    box = public.SealedBox(public.PublicKey(k["key"].encode(), encoding.Base64Encoder()))
    enc = base64.b64encode(box.encrypt(new_token.encode())).decode()
    r = requests.put(f"https://api.github.com/repos/{repo}/actions/secrets/IG_ACCESS_TOKEN", headers=h, timeout=30,
                     json={"encrypted_value": enc, "key_id": k["key_id"]})
    print("Token secret updated:", r.status_code)


def refresh_token(tok):
    try:
        r = requests.get("https://graph.instagram.com/refresh_access_token", timeout=30,
                         params={"grant_type": "ig_refresh_token", "access_token": tok})
        if r.ok and r.json().get("access_token"):
            new = r.json()["access_token"]
            update_secret(new)
            return new
        print("Token refresh skipped:", r.text[:150])
    except Exception as e:
        print("Token refresh failed:", e)
    return tok


def post():
    meta = json.loads(META.read_text())
    repo, branch = os.environ["GITHUB_REPOSITORY"], os.environ.get("GITHUB_REF_NAME", "main")
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/videos/{meta['file']}"
    for _ in range(24):
        if requests.head(url, timeout=20).status_code == 200:
            break
        time.sleep(10)
    tok = refresh_token(os.environ["IG_ACCESS_TOKEN"])
    base = f"https://graph.instagram.com/{GV}"

    def call(method, path, **kw):
        r = requests.request(method, f"{base}/{path}", timeout=60, **kw)
        if not r.ok:
            sys.exit(f"Instagram error {r.status_code}: {r.text[:400]}")
        return r.json()

    me = call("GET", "me", params={"fields": "user_id,username", "access_token": tok})
    uid = me.get("user_id") or me["id"]
    cid = call("POST", f"{uid}/media", data={"media_type": "REELS", "video_url": url, "share_to_feed": "true",
                                              "caption": meta["caption"], "access_token": tok})["id"]
    for _ in range(40):
        st = call("GET", cid, params={"fields": "status_code", "access_token": tok}).get("status_code")
        print("Status:", st)
        if st == "FINISHED":
            break
        if st in ("ERROR", "EXPIRED"):
            sys.exit("Instagram could not process the video")
        time.sleep(15)
    res = call("POST", f"{uid}/media_publish", data={"creation_id": cid, "access_token": tok})
    print("Published! id:", res.get("id"))


if __name__ == "__main__":
    {"make": make, "post": post}[sys.argv[1]]()
