import math
import shutil
import struct
import subprocess
import wave

import pytest


def make_wav(path, seconds=1):
    sample_rate = 8000
    frame_count = sample_rate * seconds
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for index in range(frame_count):
            sample = int(16000 * math.sin(2 * math.pi * 440 * index / sample_rate))
            wav.writeframes(struct.pack("<h", sample))
    return path


def make_mp4(path, seconds=1, size="160x90"):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required to generate a tiny mp4 fixture")

    command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={size}:rate=2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:sample_rate=8000",
        "-t",
        str(seconds),
        "-c:v",
        "mpeg4",
        "-c:a",
        "aac",
        str(path),
    ]
    result = subprocess.run(command, text=True, capture_output=True, timeout=30)
    if result.returncode != 0:
        pytest.skip(f"ffmpeg could not create mp4 fixture: {result.stderr}")
    return path
