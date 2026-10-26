# demo

## YouTube voiceover generator

`voiceover.py` turns a written script into a voiceover ready to drop into your editor:

- **`<name>.mp3`**: the narration, with natural pauses, light compression, and loudness set to
  YouTube's -14 LUFS. Optionally it adds a background music track that dips while you talk.
- **`<name>.srt`**: captions timed to the audio, to upload in YouTube Studio under *Subtitles*.
- **`<name>.chapters.txt`**: chapter timestamps (`0:00 Intro`, …) to paste into the video
  description. The script warns you if they break YouTube's chapter rules.

### Setup

Requires Python 3.9+ and [ffmpeg](https://ffmpeg.org/download.html).

```sh
pip install -r requirements.txt
```

### Usage

```sh
python voiceover.py examples/coffee.md                   # -> examples/coffee.mp3 / .srt / .chapters.txt
python voiceover.py script.md -o out/episode12.mp3 --voice en-GB-RyanNeural --rate +5%
python voiceover.py script.md --music lofi.mp3 --music-volume 0.1
python voiceover.py --list-voices en                     # browse voices
```

Each sentence is cached in `.voiceover_cache/` next to the output, so when you edit a line and
re-run, only the changed sentences are synthesised again.

### Writing the script

Write plain text or Markdown, the way you would say it:

```markdown
# Intro
Welcome back! Today we're fixing the three biggest coffee mistakes.

// B-roll: beans pouring into the grinder (lines starting with // are not spoken)

# Fresh beans
Coffee starts losing its aroma within minutes of being ground. [pause 0.5] So grind just before you brew.
```

| Syntax         | Effect                                                    |
| -------------- | --------------------------------------------------------- |
| `# Heading`    | Starts a chapter. Headings are not spoken.                |
| blank line     | New paragraph, with a slightly longer pause (`--paragraph-gap`, 0.7s) |
| `[pause 1.5]`  | Silence of the given seconds (`[pause]` = 1s)             |
| `// note`      | Ignored                                                   |

Write numbers and acronyms the way you want them read out ("ninety degrees", "A P I").

### Voices

**`--engine edge` (default)** uses Microsoft Edge's free neural voices through
[edge-tts](https://github.com/rany2/edge-tts). It needs an internet connection and has hundreds
of voices across many languages and accents. Popular narration voices include
`en-US-AndrewNeural` (default), `en-US-BrianNeural`, `en-US-AvaNeural`, `en-GB-RyanNeural` and
`en-GB-SoniaNeural`. Adjust the delivery with `--rate +8%` or `--pitch -2Hz`.

**`--engine piper`** runs fully offline with [Piper](https://github.com/OHF-Voice/piper1-gpl). Download
a voice once, then point `--voice` at its `.onnx` file:

```sh
python -m piper.download_voices en_US-ryan-high
python voiceover.py script.md --engine piper --voice en_US-ryan-high.onnx
```

Before relying on a voice in a monetised video, check its license and the platform's terms.

### All options

Run `python voiceover.py --help`. Useful ones: `--sentence-gap`, `--paragraph-gap`,
`--no-normalize` (skip loudness levelling), `--jobs` (sentences synthesised in parallel), and
`-o file.wav` for uncompressed output.
