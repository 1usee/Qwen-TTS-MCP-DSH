import base64
import os
import time
import pytest
from pathlib import Path
from qwen_voice_mcp.core import audio_utils as au
from qwen_voice_mcp.core.models import Status


def test_make_tone_wav_then_probe_reports_duration(tmp_path):
    wav = au.make_tone_wav(tmp_path / "a.wav", seconds=2.5)
    p = au.probe(wav)
    assert p.format == "wav"
    assert p.duration_seconds == pytest.approx(2.5, abs=0.05)
    assert p.sample_rate == 16000
    assert p.channels == 1


def test_probe_missing_file_returns_duration_none(tmp_path):
    p = au.probe(tmp_path / "nope.wav")
    assert p.duration_seconds is None


def test_probe_of_non_audio_bytes_returns_duration_none(tmp_path):
    f = tmp_path / "fake.wav"
    f.write_bytes(b"not audio at all" * 10)
    assert au.probe(f).duration_seconds is None


def test_detect_backend_is_a_known_name():
    assert au.detect_backend() in {"soundfile", "pydub", "stdlib"}


def test_validate_accepts_in_range_audio(tmp_path):
    wav = au.make_tone_wav(tmp_path / "ok.wav", seconds=10)
    r = au.validate_reference(wav)
    assert r.status is Status.SUCCESS


def test_validate_rejects_too_short(tmp_path):
    wav = au.make_tone_wav(tmp_path / "short.wav", seconds=4.9)
    r = au.validate_reference(wav)
    assert r.status is Status.INVALID_INPUT
    assert "5" in r.message


def test_validate_rejects_too_long(tmp_path):
    wav = au.make_tone_wav(tmp_path / "long.wav", seconds=60.5)
    r = au.validate_reference(wav)
    assert r.status is Status.INVALID_INPUT


def test_validate_accepts_exact_boundaries(tmp_path):
    lo = au.make_tone_wav(tmp_path / "lo.wav", seconds=5.0)
    hi = au.make_tone_wav(tmp_path / "hi.wav", seconds=60.0)
    assert au.validate_reference(lo).status is Status.SUCCESS
    assert au.validate_reference(hi).status is Status.SUCCESS


def test_validate_rejects_disallowed_extension(tmp_path):
    f = tmp_path / "x.txt"
    f.write_bytes(b"hi")
    r = au.validate_reference(f)
    assert r.status is Status.INVALID_INPUT


def test_validate_rejects_oversized_file(tmp_path):
    wav = au.make_tone_wav(tmp_path / "big.wav", seconds=10)
    r = au.validate_reference(wav, max_bytes=100)
    assert r.status is Status.INVALID_INPUT
    assert "大小" in r.message or "MB" in r.message


def test_validate_rejects_missing_file(tmp_path):
    assert au.validate_reference(tmp_path / "gone.wav").status is Status.INVALID_INPUT


def test_validate_unknown_duration_is_warning_not_failure(tmp_path, monkeypatch):
    f = tmp_path / "m.m4a"
    f.write_bytes(b"\x00\x00\x00\x20ftypM4A " + os.urandom(64))
    r = au.validate_reference(f)
    assert r.status is Status.SUCCESS
    assert r.warnings


def test_materialise_accepts_plain_path(tmp_path):
    wav = au.make_tone_wav(tmp_path / "a.wav", seconds=6)
    assert au.materialise_audio(str(wav), tmp_path) == wav


def test_materialise_decodes_data_uri(tmp_path):
    raw = (au.make_tone_wav(tmp_path / "s.wav", seconds=6)).read_bytes()
    uri = "data:audio/wav;base64," + base64.b64encode(raw).decode()
    out = au.materialise_audio(uri, tmp_path)
    assert out.suffix == ".wav" and out.read_bytes() == raw


def test_materialise_decodes_bare_base64(tmp_path):
    raw = (au.make_tone_wav(tmp_path / "s.wav", seconds=6)).read_bytes()
    out = au.materialise_audio(base64.b64encode(raw).decode(), tmp_path)
    assert out.read_bytes() == raw


def test_materialise_rejects_garbage_without_raising(tmp_path):
    with pytest.raises(ValueError):
        au.materialise_audio("!!!not base64 and not a path!!!", tmp_path)


def test_new_output_path_is_unique_and_keeps_format(tmp_path):
    a = au.new_output_path(tmp_path, "mp3")
    b = au.new_output_path(tmp_path, "mp3")
    assert a != b
    assert a.suffix == ".mp3" and a.parent == tmp_path


def test_cleanup_removes_only_stale_files(tmp_path):
    old = tmp_path / "old.mp3"; old.write_bytes(b"x")
    new = tmp_path / "new.mp3"; new.write_bytes(b"x")
    past = time.time() - 48 * 3600
    os.utime(old, (past, past))
    assert au.cleanup_temp_files(tmp_path, max_age_hours=24) == 1
    assert new.exists() and not old.exists()
