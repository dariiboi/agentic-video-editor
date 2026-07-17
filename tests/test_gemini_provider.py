import json
from types import SimpleNamespace

from agentic_video_editor import gemini_provider
from agentic_video_editor.gemini_provider import GeminiProvider, MockProvider


class FakeFiles:
    def __init__(self):
        self.upload_count = 0
        self.deleted = []

    def upload(self, file):
        del file
        self.upload_count += 1
        return SimpleNamespace(name=f"files/fake-{self.upload_count}", state=SimpleNamespace(name="ACTIVE"))

    def get(self, name):
        return SimpleNamespace(name=name, state=SimpleNamespace(name="ACTIVE"))

    def delete(self, name):
        self.deleted.append(name)


class FakeModels:
    def __init__(self, errors=None):
        self.calls = 0
        self.errors = list(errors or [])

    def generate_content(self, model, contents, config):
        del model, contents, config
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return SimpleNamespace(text=json.dumps({"call": self.calls}))


class FakeClient:
    def __init__(self, models=None):
        self.files = FakeFiles()
        self.models = models or FakeModels()


def _session_provider(monkeypatch, fake):
    monkeypatch.setattr(gemini_provider, "_build_client", lambda env_path: fake)
    monkeypatch.setattr(gemini_provider.time, "sleep", lambda seconds: None)
    return GeminiProvider()


def _video(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video bytes")
    return video


def test_video_session_uploads_once_for_many_prompts(monkeypatch, tmp_path):
    fake = FakeClient()
    provider = _session_provider(monkeypatch, fake)
    with provider.video_session(_video(tmp_path)) as session:
        first = session.generate_json("facet one")
        second = session.generate_json("facet two")
        third = session.generate_json("facet three")
    assert (first, second, third) == ({"call": 1}, {"call": 2}, {"call": 3})
    assert fake.files.upload_count == 1
    assert fake.files.deleted == ["files/fake-1"]


def test_retryable_generate_failure_retries_without_reupload(monkeypatch, tmp_path):
    fake = FakeClient(models=FakeModels(errors=[RuntimeError("503 service unavailable")]))
    provider = _session_provider(monkeypatch, fake)
    with provider.video_session(_video(tmp_path)) as session:
        result = session.generate_json("facet one")
    assert result == {"call": 2}
    assert fake.files.upload_count == 1
    assert fake.files.deleted == ["files/fake-1"]


def test_dead_file_handle_triggers_reupload(monkeypatch, tmp_path):
    fake = FakeClient(
        models=FakeModels(errors=[RuntimeError("File files/fake-1 is not in an ACTIVE state")])
    )
    provider = _session_provider(monkeypatch, fake)
    with provider.video_session(_video(tmp_path)) as session:
        result = session.generate_json("facet one")
    assert result == {"call": 2}
    assert fake.files.upload_count == 2
    assert fake.files.deleted == ["files/fake-1", "files/fake-2"]


def test_generate_video_json_still_works_through_session(monkeypatch, tmp_path):
    fake = FakeClient()
    provider = _session_provider(monkeypatch, fake)
    result = provider.generate_video_json(_video(tmp_path), "one-shot prompt")
    assert result == {"call": 1}
    assert fake.files.upload_count == 1
    assert fake.files.deleted == ["files/fake-1"]


def test_mock_provider_session_matches_generate_video_json(tmp_path):
    provider = MockProvider()
    video = tmp_path / "clip.mp4"
    with provider.video_session(video) as session:
        result = session.generate_json("anything")
    assert result == provider.generate_video_json(video, "anything")
    assert result["segments"][0]["summary"] == "Usable opening moment from clip."
