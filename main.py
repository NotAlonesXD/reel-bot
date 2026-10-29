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
    r = None
    for model in dict.fromkeys([MODEL, "gemini-flash-latest"]):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(1, 6):
            try:
                r = requests.post(url, params={"key": GEMINI_KEY}, timeout=900, json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0}})
            except requests.exceptions.Timeout:
                print(f"Gemini ({model}) timed out, try {attempt}/5")
                time.sleep(20)
                continue
            if r.ok:
                break
            print(f"Gemini ({model}) error {r.status_code}, try {attempt}/5")
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(30 * attempt)   # busy server: wait and try again
                continue
            break                          # other errors: try the next model
        if r is not None and r.ok:
            break
    if r is None or not r.ok:
        sys.exit(f"Gemini failed: {r.status_code if r is not None else 'no answer'} {r.text[:300] if r is not None else ''}")
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


# ---------- 2. voice + word timings ----------
async def tts_edge(text, path, voice):
    import edge_tts
    com = edge_tts.Communicate(text, voice, boundary="WordBoundary")
    words = []
    with open(path, "wb") as f:
        async for ch in com.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                s = ch["offset"] / 1e7
                words.append((ch["text"], s, s + ch["duration"] / 1e7))
    if not words:
        raise RuntimeError("no words returned")
    return words


def estimate_words(text, dur):
    """Spread the words over the audio length (used when the voice gives no timings)."""
    ws = text.split()
    total = sum(len(w) + 2 for w in ws)
    t, out, span = 0.2, [], max(dur - 0.6, 1)
    for w in ws:
        d = span * (len(w) + 2) / total
        out.append((w, t, t + d))
        t += d
    return out


PIPER_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium"


def tts_piper(text, path):
    for ext in (".onnx", ".onnx.json"):
        with requests.get(PIPER_URL + ext, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(TMP / f"piper{ext}", "wb") as f:
                for b in r.iter_content(1 << 16):
                    f.write(b)
    wav = TMP / "voice.wav"
    run([sys.executable, "-m", "piper", "-m", str(TMP / "piper.onnx"), "-f", str(wav)], input=text.encode())
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-ar", "44100", str(path)])
    dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                         "-of", "csv=p=0", str(path)]).decode().strip())
    return estimate_words(text, dur)


def tts(text, path):
    for voice in (VOICE, "en-US-GuyNeural", "en-GB-RyanNeural"):
        for _ in range(2):
            try:
                return asyncio.run(tts_edge(text, path, voice))
            except Exception as e:
                print(f"edge-tts failed with {voice}: {type(e).__name__}")
                time.sleep(3)
    print("Switching to backup voice (Piper)")
    return tts_piper(text, path)


# ---------- 3. captions (ASS file) ----------
def ts(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def make_ass(words, path, per=3):
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n"
            "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
            "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,"
            "Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
            "Style: Default,DejaVu Sans,96,&H0000D7FF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,5,60,60,0,1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
    pop = "{\\an5\\pos(540,1200)\\fscx82\\fscy82\\t(0,110,\\fscx108\\fscy108)\\t(110,200,\\fscx100\\fscy100)}"
    chunks = [words[i:i + per] for i in range(0, len(words), per)]
    lines = []
    for i, ch in enumerate(chunks):
        start = ch[0][1]
        end = chunks[i + 1][0][1] if i + 1 < len(chunks) else ch[-1][2] + 0.4
        parts = []
        for j, w in enumerate(ch):
            nxt = ch[j + 1][1] if j + 1 < len(ch) else end
            cs = max(5, int((nxt - w[1]) * 100))
            word = w[0].upper().replace("{", "").replace("}", "").replace("\\", "")
            parts.append("{\\kf" + str(cs) + "}" + word + " ")
        lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Default,,0,0,0,," + pop + "".join(parts).strip())
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
XFADES = ["fade", "slideleft", "slideright", "wipeleft", "circleopen", "zoomin", "fadeblack", "smoothleft"]
D = 0.3  # transition length


def seg_count(dur):
    return max(5, math.ceil(dur / 3.5))


def make_sfx(dur, n, seg):
    """Whoosh on every cut + a low boom at the start (all synthesized, no copyright)."""
    ff = ["ffmpeg", "-y", "-loglevel", "error"]
    run(ff + ["-f", "lavfi", "-i", "anoisesrc=d=0.7:c=pink:a=0.6", "-af",
              "highpass=f=200,lowpass=f=5000,afade=t=in:st=0:d=0.35,afade=t=out:st=0.35:d=0.35,volume=1.3",
              str(TMP / "whoosh.wav")])
    run(ff + ["-f", "lavfi", "-i", "sine=f=55:d=0.9", "-af", "afade=t=out:st=0.05:d=0.85,volume=2.5",
              str(TMP / "boom.wav")])
    cmd, flt, labels = list(ff), [], []
    cmd += ["-i", str(TMP / "boom.wav")]
    flt.append("[0:a]adelay=0|0[a0]")
    labels.append("[a0]")
    for k in range(1, n):
        cmd += ["-i", str(TMP / "whoosh.wav")]
        ms = max(0, int((k * seg - 0.25) * 1000))
        flt.append(f"[{k}:a]adelay={ms}|{ms}[a{k}]")
        labels.append(f"[a{k}]")
    flt.append("".join(labels) + f"amix=inputs={n}:normalize=0:duration=longest,apad=whole_dur={dur:.2f}[o]")
    run(cmd + ["-filter_complex", ";".join(flt), "-map", "[o]", "-t", f"{dur:.2f}", str(TMP / "sfx.wav")])


def make_audio(voice, dur, n, seg):
    make_sfx(dur, n, seg)
    tracks = sorted((ROOT / "music").glob("*.mp3")) if (ROOT / "music").exists() else []
    ff = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(pathlib.Path(voice).resolve()),
          "-i", str(TMP / "sfx.wav")]
    if tracks:
        track = random.choice(tracks)
        print("Music:", track.name)
        ff += ["-stream_loop", "-1", "-i", str(track)]
        mv = 0.35
    else:
        print("No music files found, using a soft synthesized background")
        for f in (110, 164.81, 220, 277.18):
            ff += ["-f", "lavfi", "-i", f"sine=f={f}:d={dur:.2f}"]
        mv = 1.0
    music = "[2:a]volume=%s" % mv if tracks else \
        "[2:a][3:a][4:a][5:a]amix=inputs=4:normalize=0,lowpass=f=900,tremolo=f=0.15:d=0.6,volume=0.18"
    flt = (f"[0:a]apad=whole_dur={dur:.2f},asplit=2[v1][v2];{music}[m0];"
           f"[m0][v2]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=300[md];"
           f"[v1][md][1:a]amix=inputs=3:duration=longest:normalize=0,"
           f"afade=t=in:st=0:d=0.3,afade=t=out:st={dur - 0.5:.2f}:d=0.5[o]")
    run(ff + ["-filter_complex", flt, "-map", "[o]", "-t", f"{dur:.2f}", str(TMP / "mix.wav")])


def build_video(clips, audio, ass_name, dur, out):
    n = seg_count(dur)
    seg = dur / n
    L = seg + D
    for i in range(n):
        d = 1 if i % 2 == 0 else -1
        src = clips[i % len(clips)]
        vf = ("scale=1188:2112:force_original_aspect_ratio=increase,"
              f"crop=1080:1920:x='(iw-1080)/2+({d})*160*(t/{L:.2f}-0.5)':y='(ih-1920)/2',fps=30,setsar=1")
        run(["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-i", str(src), "-t", f"{L:.2f}",
             "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
             str(TMP / f"seg{i}.mp4")])
    make_audio(audio, dur, n, seg)
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for i in range(n):
        cmd += ["-i", str(TMP / f"seg{i}.mp4")]
    cmd += ["-i", str(TMP / "mix.wav"),
            "-f", "lavfi", "-i", f"color=c=0xFFD700:s=1080x14:r=30:d={dur:.2f}"]
    flt, prev = [], "[0:v]"
    for k in range(1, n):
        tr = random.choice(XFADES)
        flt.append(f"{prev}[{k}:v]xfade=transition={tr}:duration={D}:offset={k * seg:.3f}[x{k}]")
        prev = f"[x{k}]"
    flt.append(f"{prev}eq=contrast=1.08:saturation=1.15:brightness=-0.07,vignette=PI/5,{'ass=' + ass_name}[g]")
    flt.append(f"[g][{n + 1}:v]overlay=x='-overlay_w+overlay_w*t/{dur:.2f}':y=1750[o2]")
    flt.append(f"[o2]fade=t=in:st=0:d=0.3,fade=t=out:st={dur - 0.4:.2f}:d=0.4[vout]")
    cmd += ["-filter_complex", ";".join(flt), "-map", "[vout]", "-map", f"{n}:a", "-t", f"{dur:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(pathlib.Path(out).resolve())]
    run(cmd, cwd=TMP)


def make():
    TMP.mkdir(exist_ok=True)
    VIDEOS.mkdir(exist_ok=True)
    data = make_script()
    narration = " ".join([data["hook"]] + data["facts"] + [data["outro"]])
    print("TOPIC:", data["topic"], "\n", narration)
    words = tts(narration, TMP / "voice.mp3")
    make_ass(words, TMP / "captions.ass")
    dur = words[-1][2] + 0.8
    clips = fetch_clips(data["search_terms"], min(seg_count(dur), 10))
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
            
