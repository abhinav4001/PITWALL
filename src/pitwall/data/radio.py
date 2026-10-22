"""Team radio: the calls, their audio, and their transcript.

How this works, and what had to be measured to get it right:

* OpenF1's endpoint is ``/team_radio`` - **not** ``/radio``, which returns
  404 and looks like "the free tier has no radio".  The free tier does carry
  radio for the sessions F1 published it for.
* Each row gives a ``recording_url`` pointing at the official F1 static
  archive.  Those **audio files are still served without authentication**,
  even though the feed's index (``/static/<year>/Index.json``) and its live
  SignalR socket both answer ``HTTP 401``.  So the radio can be played and
  transcribed without any subscription.
* OpenF1 publishes **audio, not text**.  The transcript is produced here, on
  this machine, with Vosk (a small offline speech recogniser).  Nothing is
  sent anywhere.

F1 does not release radio for every session: coverage fell sharply during
2026 and most events carry none at all.  When there is nothing, the API says
so plainly rather than showing an empty box that looks broken.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pitwall.data.openf1 import OpenF1Client

logger = logging.getLogger(__name__)

#: Where the speech model lives.  It is downloaded on demand.
MODEL_DIR = Path(os.path.expanduser("~/vosk-models"))

#: The model used for the transcripts.  ``small`` is 40 MB and rough;
#: ``en-us-0.22-lgraph`` is 128 MB and noticeably better.  On the 2024
#: United States Grand Prix the larger model got "the stewards are looking at
#: a max" right where the small one heard "a bucks", and "how's the pace"
#: where the small one heard "how's the pies".
MODEL_NAME = os.environ.get("PITWALL_RADIO_MODEL", "vosk-model-en-us-0.22-lgraph")

#: Where the model is fetched from.
MODEL_URLS = {
    "vosk-model-en-us-0.22-lgraph": (
        "https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip"
    ),
    "vosk-model-small-en-us-0.15": (
        "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
    ),
}


@dataclass
class RadioMessage:
    """One radio transmission."""

    date: str | None
    driver_number: int | None
    team: str | None
    text: str
    recording_url: str | None = None
    duration: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "driver_number": self.driver_number,
            "team": self.team,
            "text": self.text,
            "recording_url": self.recording_url,
            "duration": self.duration,
        }


@dataclass
class RadioTranscript:
    """A transcript plus where it came from."""

    messages: list[RadioMessage] = field(default_factory=list)
    source: str = "unavailable"
    reason: str = ""

    @property
    def available(self) -> bool:
        return bool(self.messages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "source": self.source if self.available else "unavailable",
            "reason": "" if self.available else self.reason,
            "count": len(self.messages),
            "messages": [message.to_dict() for message in self.messages],
        }


# ----------------------------------------------------------------------
# fetching
# ----------------------------------------------------------------------


async def fetch_team_radio(
    client: OpenF1Client, session_key: int
) -> list[RadioMessage]:
    """The radio calls OpenF1 published for a session."""
    rows = await client.get_team_radio(session_key)
    messages: list[RadioMessage] = []
    for row in rows:
        url = (row.get("recording_url") or "").strip()
        if not url:
            continue
        messages.append(
            RadioMessage(
                date=row.get("date"),
                driver_number=(
                    int(row["driver_number"])
                    if row.get("driver_number") is not None
                    else None
                ),
                team=None,
                text="",
                recording_url=url,
            )
        )
    messages.sort(key=lambda message: str(message.date or ""))
    return messages


def radio_message_from_row(row: dict[str, Any]) -> RadioMessage | None:
    """One cached radio row as a :class:`RadioMessage`."""
    url = (row.get("recording_url") or "").strip()
    if not url:
        return None
    return RadioMessage(
        date=row.get("date"),
        driver_number=(
            int(row["driver_number"]) if row.get("driver_number") is not None else None
        ),
        team=row.get("team_name") or row.get("team") or None,
        text=(row.get("transcript") or "").strip(),
        recording_url=url,
    )


def load_cached_radio(directory: str | Path) -> list[RadioMessage]:
    """Radio cached alongside a session pack."""
    path = Path(directory) / "radio.json"
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    messages: list[RadioMessage] = []
    for row in rows:
        message = radio_message_from_row(row)
        if message is not None:
            messages.append(message)
    messages.sort(key=lambda message: str(message.date or ""))
    return messages


def save_radio(directory: str | Path, messages: list[RadioMessage]) -> None:
    path = Path(directory) / "radio.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "date": message.date,
                    "driver_number": message.driver_number,
                    "team_name": message.team,
                    "transcript": message.text,
                    "recording_url": message.recording_url,
                    "duration": message.duration,
                }
                for message in messages
            ],
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


# ----------------------------------------------------------------------
# audio
# ----------------------------------------------------------------------


def _ffmpeg_exe() -> str | None:
    """The ffmpeg binary used to decode the recordings.

    ``imageio-ffmpeg`` bundles the executable, which is preferable to
    depending on ffmpeg being on ``PATH``.
    """
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - the binary is optional
        return None


def _decode_to_wav(data: bytes, wav_path: Path) -> bool:
    """Turn an MP3 into 16 kHz mono PCM, which is what Vosk expects."""
    exe = _ffmpeg_exe()
    if exe is None:
        return False
    try:
        result = subprocess.run(
            [exe, "-y", "-i", "pipe:0", "-ar", "16000", "-ac", "1", str(wav_path)],
            input=data,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and wav_path.exists()


# ----------------------------------------------------------------------
# transcription
# ----------------------------------------------------------------------


def model_path(name: str = MODEL_NAME) -> Path:
    return MODEL_DIR / name


def ensure_model(name: str = MODEL_NAME, *, download: bool = True) -> Path | None:
    """Make sure the speech model is on disk, downloading it if needed."""
    path = model_path(name)
    if path.exists():
        return path
    if not download:
        return None
    url = MODEL_URLS.get(name)
    if url is None:
        return None
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    archive = MODEL_DIR / f"{name}.zip"
    logger.info("downloading the speech model %s", name)
    try:
        import httpx

        with httpx.stream(
            "GET", url, timeout=300.0, follow_redirects=True
        ) as response:
            response.raise_for_status()
            with archive.open("wb") as handle:
                for chunk in response.iter_bytes(65536):
                    handle.write(chunk)
    except Exception as exc:  # noqa: BLE001 - the transcript is optional
        logger.warning("could not download the speech model: %s", exc)
        return None
    try:
        import zipfile

        with zipfile.ZipFile(archive) as zf:
            zf.extractall(MODEL_DIR)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not unpack the speech model: %s", exc)
        return None
    finally:
        archive.unlink(missing_ok=True)
    return path if path.exists() else None


_MODEL_CACHE: dict[str, Any] = {}


def _load_model(name: str = MODEL_NAME) -> Any | None:
    if name in _MODEL_CACHE:
        return _MODEL_CACHE[name]
    path = ensure_model(name)
    if path is None:
        return None
    try:
        from vosk import Model, SetLogLevel

        SetLogLevel(-1)
        model = Model(str(path))
    except Exception as exc:  # noqa: BLE001 - the transcript is optional
        logger.warning("could not load the speech model: %s", exc)
        return None
    _MODEL_CACHE[name] = model
    return model


def transcribe_mp3(data: bytes, work_dir: Path, model: Any) -> str:
    """Transcribe one MP3 recording.  Returns the spoken text."""
    work_dir.mkdir(parents=True, exist_ok=True)
    wav_path = work_dir / "clip.wav"
    try:
        if not _decode_to_wav(data, wav_path):
            return ""
        with wave.open(str(wav_path), "rb") as handle:
            from vosk import KaldiRecognizer

            recognizer = KaldiRecognizer(model, handle.getframerate())
            recognizer.SetWords(True)
            while True:
                chunk = handle.readframes(4000)
                if not chunk:
                    break
                recognizer.AcceptWaveform(chunk)
            result = json.loads(recognizer.FinalResult())
        text = str(result.get("text") or "").strip()
        words = result.get("result") or []
        duration = (words[-1]["end"] if words else 0.0) - (
            words[0]["start"] if words else 0.0
        )
        _ = duration
        return text
    except Exception as exc:  # noqa: BLE001 - never break the radio section
        logger.warning("transcription failed: %s", exc)
        return ""
    finally:
        wav_path.unlink(missing_ok=True)


async def transcribe_all(
    messages: list[RadioMessage],
    *,
    work_dir: Path,
    limit: int | None = None,
    model_name: str = MODEL_NAME,
    on_done: Any = None,
) -> list[RadioMessage]:
    """Fetch the recordings and transcribe them.

    The audio is fetched from F1's public static archive and transcribed
    locally; nothing is sent to a third party.  Transcriptions are cached on
    disk so a session is only ever processed once.

    ``on_done`` is called with the finished list after every clip, so a
    caller can publish the results as they arrive rather than waiting for
    the whole session.
    """
    model = _load_model(model_name)
    if model is None:
        logger.info("no speech model available; radio will show without transcripts")
        return messages

    import httpx

    cache_dir = work_dir / "transcripts"
    cache_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:

        async def one(message: RadioMessage) -> RadioMessage:
            if not message.recording_url:
                return message
            key = message.recording_url.rsplit("/", 1)[-1].removesuffix(".mp3")
            cached = cache_dir / f"{key}.txt"
            if cached.exists():
                message.text = cached.read_text(encoding="utf-8").strip()
                return message
            try:
                response = await client.get(
                    message.recording_url, headers={"User-Agent": "Mozilla/5.0"}
                )
                response.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                logger.debug("could not fetch %s: %s", message.recording_url, exc)
                return message
            text = await asyncio.to_thread(
                transcribe_mp3, response.content, work_dir, model
            )
            message.text = text
            if text:
                cached.write_text(text, encoding="utf-8")
            return message

        todo = messages if limit is None else messages[:limit]
        done: list[RadioMessage] = []
        for message in todo:
            done.append(await one(message))
            if on_done is not None:
                await on_done(done)
        return done
