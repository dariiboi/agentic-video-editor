import json

from helpers import make_mp4


def _json(result):
    return json.loads(result.stdout)


def _make_indexed_project(tmp_path, run_ave):
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    make_mp4(source_dir / "clip_0.mp4", seconds=2)
    run_ave("init", project_dir)
    run_ave("ingest", project_dir, source_dir)
    run_ave("transcribe", project_dir, "--provider", "mock")
    run_ave("semantic-analyze", project_dir, "--provider", "mock")
    run_ave("facets", project_dir, "--provider", "mock", "--json")
    return project_dir


def test_profile_reports_inventory_speech_facets_and_openers(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave)

    profile = _json(run_ave("profile", project_dir, "--json"))

    assert set(profile) == {
        "assets",
        "speech",
        "segments",
        "quotable_lines",
        "facets",
        "openers",
        "enders",
        "clusters",
    }
    assert profile["quotable_lines"]
    assert profile["quotable_lines"][0]["text"]
    assert profile["quotable_lines"][0]["end_sec"] > profile["quotable_lines"][0]["start_sec"]
    assert profile["assets"]["ready_count"] == 1
    assert profile["assets"]["total_duration_sec"] > 0
    assert profile["speech"]["basis"] in {"word_alignments", "transcript_spans"}
    assert profile["speech"]["speech_ratio"] is not None
    assert profile["segments"]["usable_count"] >= 1
    assert profile["segments"]["duration_distribution"]["median_sec"] > 0

    people = profile["facets"]["by_type"]["people_appearance"]
    terms = {entry["term"] for entry in people["top_terms"]}
    assert "green" in terms or any("green" in term for term in terms)
    assert "blue" in terms or any("blue" in term for term in terms)
    assert profile["facets"]["observation_count"] > 0

    # mock semantic select is hook-tagged, so it surfaces as an opener
    assert profile["openers"]
    assert "hook" in profile["openers"][0]["roles"]
    assert profile["openers"][0]["score"] > 0


def test_profile_markdown_contains_key_numbers(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave)

    markdown = run_ave("profile", project_dir).stdout

    assert "# Corpus Profile" in markdown
    assert "1 ready asset" in markdown
    assert "people_appearance" in markdown
    assert "green" in markdown
    assert "Strong openers" in markdown
    assert markdown.count("\n") < 80


def test_profile_of_empty_project_reports_empty(tmp_path, run_ave):
    project_dir = tmp_path / "project"
    run_ave("init", project_dir)

    profile = _json(run_ave("profile", project_dir, "--json"))
    markdown = run_ave("profile", project_dir).stdout

    assert profile["assets"]["count"] == 0
    assert profile["speech"]["basis"] == "none"
    assert profile["facets"]["observation_count"] == 0
    assert profile["openers"] == []
    assert profile["clusters"]["cluster_count"] == 0
    assert "Empty corpus: no ready assets." in markdown
