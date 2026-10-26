#!/usr/bin/env python3
"""Turn a written script into a YouTube-ready voiceover.

Writes three files next to the output audio:
  <name>.mp3           the voiceover, loudness-normalised to YouTube's -14 LUFS
  <name>.srt           captions to upload in YouTube Studio
  <name>.chapters.txt  chapter timestamps to paste into the video description

Script format (plain text or Markdown):
  # Heading       starts a chapter (not spoken)
  blank line      paragraph break (slightly longer pause)
  [pause 1.5]     silence in seconds, inline or on its own line
  // note         ignored, handy for B-roll or editing notes
"""

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 24000
CACHE_VERSION = "1"
CAPTION_LINE = 42  # YouTube's recommended maximum characters per caption line
TAIL_SECONDS = 0.5
TARGET_LUFS = -14  # YouTube turns louder uploads down to about -14 LUFS
VOICE_CHAIN = "highpass=f=70,acompressor=threshold=-20dB:ratio=3:attack=5:release=120"
TRIM_FILTER = (
    "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.03,"
    "areverse,silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.08,areverse"
)

PAUSE_RE = re.compile(r"\[pause(?:\s+(\d+(?:\.\d+)?)\s*s?)?\]", re.IGNORECASE)
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*$")
# Sentence-ending punctuation, unless the next word is lowercase (a quoted question mid-sentence).
SENTENCE_END_RE = re.compile(r"[.!?…]+[\"'”’)\]]*(?=\s+[^\sa-z]|\s*$)")
ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e", "approx", "no", "fig"}


# --- Script parsing -------------------------------------------------------

@dataclass
class Chapter:
    title: str


@dataclass
class Speech:
    text: str


@dataclass
class Pause:
    seconds: float


@dataclass
class ParagraphEnd:
    pass


def clean(text):
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # [label](url) -> label
    text = re.sub(r"[*`]|__", "", text)
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text):
    sentences, start = [], 0
    for match in SENTENCE_END_RE.finditer(text):
        word = text[start:match.start()].rsplit(" ", 1)[-1].lstrip("\"'“‘(").lower()
        if match.group() == "." and word in ABBREVIATIONS:
            continue
        sentences.append(text[start:match.end()].strip())
        start = match.end()
    sentences.append(text[start:].strip())
    return [s for s in sentences if re.search(r"\w", s)]


def parse_script(text):
    items, paragraph = [], []

    def flush():
        if not paragraph:
            return
        # With one capture group, split() alternates text, pause length, text, ...
        for i, part in enumerate(PAUSE_RE.split(" ".join(paragraph))):
            if i % 2:
                items.append(Pause(float(part) if part else 1.0))
            else:
                items.extend(Speech(s) for s in split_sentences(clean(part)))
        items.append(ParagraphEnd())
        paragraph.clear()

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("//"):
            continue
        heading = HEADING_RE.match(line)
        if heading:
            flush()
            items.append(Chapter(clean(heading.group(1))))
        elif line:
            paragraph.append(line)
        else:
            flush()
    flush()
    return items


# --- TTS engines ----------------------------------------------------------

class EdgeEngine:
    """Microsoft Edge neural voices via edge-tts: free, natural, needs internet."""

    name = "edge"
    default_voice = "en-US-AndrewNeural"

    def __init__(self, voice, rate, pitch):
        try:
            import edge_tts
        except ImportError:
            sys.exit("edge-tts is not installed: pip install -r requirements.txt")
        self.edge_tts = edge_tts
        self.voice, self.rate, self.pitch = voice or self.default_voice, rate, pitch
        self.settings = f"{self.voice}|{rate}|{pitch}"

    def synth(self, text, path):
        for attempt in range(4):
            # A Communicate can only stream once, so each retry needs a fresh one.
            communicate = self.edge_tts.Communicate(text, self.voice, rate=self.rate, pitch=self.pitch)
            try:
                return asyncio.run(communicate.save(str(path)))
            except Exception as error:
                if attempt == 3:
                    raise RuntimeError(f"edge-tts failed ({error}). It needs internet access; "
                                       "use --engine piper to work offline.") from error
                time.sleep(2 ** attempt)


class PiperEngine:
    """Piper neural voices: runs fully offline from a downloaded .onnx voice model."""

    name = "piper"

    def __init__(self, voice, rate, pitch):
        if not voice or not Path(voice).is_file():
            sys.exit("--engine piper needs --voice pointing to a Piper .onnx model (see README)")
        try:
            from piper import PiperVoice, SynthesisConfig
        except ImportError:
            sys.exit("piper-tts is not installed: pip install -r requirements.txt")
        if pitch != "+0Hz":
            print("note: --pitch is ignored by the piper engine", file=sys.stderr)
        self.voice = PiperVoice.load(voice)
        self.config = SynthesisConfig(length_scale=1 / (1 + percent(rate) / 100))
        self.settings = f"{Path(voice).resolve()}|{rate}"
        self.lock = threading.Lock()  # piper's espeak phonemizer is not thread-safe

    def synth(self, text, path):
        with self.lock, wave.open(str(path), "wb") as wav:
            self.voice.synthesize_wav(text, wav, syn_config=self.config)


ENGINES = {engine.name: engine for engine in (EdgeEngine, PiperEngine)}


def percent(rate):
    match = re.fullmatch(r"([+-]\d+)%", rate)
    if not match:
        sys.exit(f"--rate must look like +10% or -5%, got {rate!r}")
    return int(match.group(1))


def list_voices(locale):
    import edge_tts

    for voice in sorted(asyncio.run(edge_tts.list_voices()), key=lambda v: v["ShortName"]):
        if voice["Locale"].lower().startswith(locale.lower()):
            styles = ", ".join(voice.get("VoiceTag", {}).get("VoicePersonalities", []))
            print(f"{voice['ShortName']:<42} {voice['Gender']:<7} {styles}")


# --- Rendering ------------------------------------------------------------

def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def render_clips(engine, texts, cache_dir, jobs):
    """Synthesise each sentence once, caching trimmed 24 kHz mono WAVs by content hash."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    clips = {}
    for text in texts:
        key = hashlib.sha256(f"{CACHE_VERSION}|{engine.name}|{engine.settings}|{text}".encode()).hexdigest()
        clips[text] = cache_dir / f"{key[:32]}.wav"
    todo = [text for text, path in clips.items() if not path.exists()]

    def work(text):
        with tempfile.TemporaryDirectory() as tmp:
            raw, trimmed = Path(tmp) / "raw", Path(tmp) / "clip.wav"
            engine.synth(text, raw)
            ffmpeg("-i", str(raw), "-af", TRIM_FILTER, "-ac", "1", "-ar", str(SAMPLE_RATE),
                   "-c:a", "pcm_s16le", str(trimmed))
            shutil.move(trimmed, clips[text])

    if todo:
        print(f"Synthesising {len(todo)} of {len(clips)} sentences ({len(clips) - len(todo)} cached)...")
        with ThreadPoolExecutor(jobs) as pool:
            for done, _ in enumerate(pool.map(work, todo), 1):
                print(f"\r  {done}/{len(todo)}", end="", flush=True)
        print()
    return clips


def caption_chunks(sentence):
    """Split a sentence into evenly sized captions of at most two lines each."""
    target = len(sentence) / -(-len(sentence) // (2 * CAPTION_LINE))
    chunks, current = [], ""
    for word in sentence.split():
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > target:
            # Break on whichever side of this word lands closer to the target length.
            if len(candidate) - target > target - len(current):
                chunks.append(current)
                current = word
            else:
                chunks.append(candidate)
                current = ""
        else:
            current = candidate
    return chunks + [current] if current else chunks


def two_lines(chunk):
    if len(chunk) <= CAPTION_LINE or " " not in chunk:
        return chunk
    middle = len(chunk) // 2
    cut = min((i for i, c in enumerate(chunk) if c == " "), key=lambda i: abs(i - middle))
    return f"{chunk[:cut]}\n{chunk[cut + 1:]}"


def timed_cues(sentence, start, end):
    """Spread a sentence's captions across its clip in proportion to their length."""
    chunks = caption_chunks(sentence)
    total = sum(len(c) for c in chunks)
    cues, elapsed = [], 0
    for chunk in chunks:
        cue_start = start + (end - start) * elapsed / total
        elapsed += len(chunk)
        cues.append((cue_start, start + (end - start) * elapsed / total, two_lines(chunk)))
    return cues


def assemble(items, clips, sentence_gap, paragraph_gap, dest):
    """Lay the clips out on one timeline; return captions, chapters and duration."""
    frames, cues, chapters = bytearray(), [], []
    pending_gap, pending_chapter, after_speech = 0.0, None, False

    def now():
        return len(frames) / 2 / SAMPLE_RATE

    def silence(seconds):
        return bytes(2 * round(seconds * SAMPLE_RATE))

    for item in items:
        if isinstance(item, Chapter):
            pending_chapter = item.title
        elif isinstance(item, Pause):
            pending_gap, after_speech = item.seconds, False
        elif isinstance(item, ParagraphEnd):
            if after_speech:
                pending_gap = paragraph_gap
            after_speech = False
        else:
            frames += silence(pending_gap)
            start = now()
            if pending_chapter is not None:
                chapters.append((start, pending_chapter))
                pending_chapter = None
            with wave.open(str(clips[item.text]), "rb") as wav:
                frames += wav.readframes(wav.getnframes())
            cues += timed_cues(item.text, start, now())
            pending_gap, after_speech = sentence_gap, True
    frames += silence(TAIL_SECONDS)

    with wave.open(str(dest), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(frames)
    return cues, chapters, now()


def master(voice_wav, out, duration, music, music_volume, normalize):
    """Polish the voice, optionally duck a music bed under it, and normalise loudness."""
    mix = voice_wav.with_name("mix.wav")  # 32-bit float, so voice + music can't clip yet
    if music:
        graph = (
            f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={music_volume},"
            f"afade=t=out:st={max(duration - 3, 0):.2f}:d=3[music];"
            f"[0:a]{VOICE_CHAIN},aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[voice][key];"
            "[music][key]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=350[bed];"
            "[voice][bed]amix=inputs=2:duration=first:normalize=0"
        )
        ffmpeg("-i", str(voice_wav), "-stream_loop", "-1", "-i", str(music),
               "-filter_complex", graph, "-c:a", "pcm_f32le", str(mix))
    else:
        ffmpeg("-i", str(voice_wav), "-af", VOICE_CHAIN, "-ar", "48000", "-c:a", "pcm_f32le", str(mix))

    # Apply the exact gain to reach the target and let a limiter catch the few peaks
    # that would otherwise stop it (loudnorm alone falls short on spiky speech).
    # The mix is already 48 kHz: resampling after the limiter would bring the peaks back.
    gain = TARGET_LUFS - measure_loudness(mix) if normalize else 0
    limiter = f"volume={gain:.2f}dB,alimiter=limit=-2dB:level=0:latency=1"
    codec = ["-c:a", "libmp3lame", "-b:a", "192k"] if out.suffix.lower() == ".mp3" else []
    ffmpeg("-i", str(mix), "-af", limiter, *codec, str(out))


def measure_loudness(path):
    """Integrated loudness in LUFS."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(result.stderr[result.stderr.rindex("{"):])["input_i"])


def srt_time(seconds):
    ms = round(seconds * 1000)
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def write_srt(cues, path):
    blocks = [f"{i}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n" for i, (start, end, text) in enumerate(cues, 1)]
    path.write_text("\n".join(blocks), encoding="utf-8")


def chapter_time(seconds, with_hours):
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if with_hours else f"{minutes}:{secs:02d}"


def write_chapters(chapters, duration, path):
    """Write YouTube description chapters and return any rule YouTube would reject them for."""
    chapters = [(0.0, chapters[0][1])] + chapters[1:]  # YouTube requires the first one at 0:00
    lines = [f"{chapter_time(start, duration >= 3600)} {title}" for start, title in chapters]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    problems = []
    if len(chapters) < 3:
        problems.append("YouTube needs at least 3 chapters")
    ends = [start for start, _ in chapters[1:]] + [duration]
    short = [title for (start, title), end in zip(chapters, ends) if end - start < 10]
    if short:
        problems.append(f"YouTube needs chapters of 10s or more; too short: {', '.join(short)}")
    return problems


# --- CLI ------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("script", nargs="?", type=Path, help="script file (.txt or .md)")
    parser.add_argument("-o", "--output", type=Path, help="output audio (.mp3, .wav or .m4a); default: <script>.mp3")
    parser.add_argument("--engine", choices=ENGINES, default="edge", help="TTS engine (default: edge)")
    parser.add_argument("--voice", help=f"edge voice name (default: {EdgeEngine.default_voice}) or piper .onnx model path")
    # Negative values must be written --rate=-5% so argparse doesn't read them as a flag.
    parser.add_argument("--rate", default="+0%", help="speaking rate, e.g. --rate=+10%% or --rate=-5%%")
    parser.add_argument("--pitch", default="+0Hz", help="edge only: pitch shift, e.g. --pitch=-2Hz")
    parser.add_argument("--sentence-gap", type=float, default=0.25, help="seconds between sentences (default: 0.25)")
    parser.add_argument("--paragraph-gap", type=float, default=0.7, help="seconds between paragraphs (default: 0.7)")
    parser.add_argument("--music", type=Path, help="background music, looped and ducked under the voice")
    parser.add_argument("--music-volume", type=float, default=0.12, help="music level before ducking (default: 0.12)")
    parser.add_argument("--no-normalize", action="store_true", help="skip loudness normalisation to -14 LUFS")
    parser.add_argument("--jobs", type=int, default=4, help="sentences to synthesise in parallel (default: 4)")
    parser.add_argument("--list-voices", nargs="?", const="", metavar="LOCALE",
                        help="list edge voices, optionally for a locale such as en-US, and exit")
    args = parser.parse_args()
    if args.list_voices is None and not args.script:
        parser.error("a script file is required")
    return args


def main():
    args = parse_args()
    if args.list_voices is not None:
        return list_voices(args.list_voices)
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is required: https://ffmpeg.org/download.html")
    if args.music and not args.music.is_file():
        sys.exit(f"music file not found: {args.music}")

    out = args.output or args.script.with_suffix(".mp3")
    out.parent.mkdir(parents=True, exist_ok=True)
    items = parse_script(args.script.read_text(encoding="utf-8"))
    sentences = list(dict.fromkeys(item.text for item in items if isinstance(item, Speech)))
    if not sentences:
        sys.exit("the script has nothing to say")

    engine = ENGINES[args.engine](args.voice, args.rate, args.pitch)
    try:
        clips = render_clips(engine, sentences, out.parent / ".voiceover_cache", args.jobs)
    except RuntimeError as error:
        sys.exit(f"\nerror: {error}")

    with tempfile.TemporaryDirectory() as tmp:
        voice_wav = Path(tmp) / "voice.wav"
        cues, chapters, duration = assemble(items, clips, args.sentence_gap, args.paragraph_gap, voice_wav)
        master(voice_wav, out, duration, args.music, args.music_volume, not args.no_normalize)

    srt = out.with_suffix(".srt")
    write_srt(cues, srt)
    print(f"Voiceover: {out} ({chapter_time(duration, duration >= 3600)})")
    print(f"Captions:  {srt}")
    if chapters:
        chapters_txt = out.with_name(f"{out.stem}.chapters.txt")
        problems = write_chapters(chapters, duration, chapters_txt)
        print(f"Chapters:  {chapters_txt}")
        for problem in problems:
            print(f"  warning: {problem}")


if __name__ == "__main__":
    main()
