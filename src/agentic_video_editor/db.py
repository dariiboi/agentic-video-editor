from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_VERSION = 3


def connect_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists schema_migrations (
            version integer primary key,
            applied_at text not null
        );

        create table if not exists projects (
            id text primary key,
            name text not null,
            root_path text not null,
            schema_version integer not null,
            config_json text not null,
            created_at text not null,
            updated_at text not null
        );

        create table if not exists assets (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            path text not null,
            file_name text not null,
            extension text not null,
            size_bytes integer not null,
            mtime_ns integer not null,
            sha256 text not null,
            duration_sec real,
            fps real,
            width integer,
            height integer,
            video_codec text,
            audio_codec text,
            has_audio integer not null default 0,
            created_at_source text,
            ingest_status text not null,
            probe_status text not null,
            probe_error text,
            probe_json text,
            first_seen_at text not null,
            updated_at text not null,
            unique(project_id, path)
        );

        create index if not exists idx_assets_project_path
            on assets(project_id, path);

        create index if not exists idx_assets_sha256
            on assets(sha256);

        create table if not exists ingest_runs (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            source_paths_json text not null,
            status text not null,
            files_found integer not null default 0,
            assets_created integer not null default 0,
            assets_updated integer not null default 0,
            assets_unchanged integer not null default 0,
            error text,
            started_at text not null,
            finished_at text
        );

        create table if not exists run_events (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            run_id text references ingest_runs(id) on delete cascade,
            asset_id text references assets(id) on delete set null,
            agent text,
            event_type text not null,
            message text not null,
            payload_json text,
            created_at text not null
        );

        create table if not exists analysis_runs (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            kind text not null,
            status text not null,
            params_json text not null,
            assets_requested integer not null default 0,
            assets_completed integer not null default 0,
            error text,
            started_at text not null,
            finished_at text
        );

        create table if not exists media_artifacts (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            asset_id text not null references assets(id) on delete cascade,
            artifact_type text not null,
            path text not null,
            metadata_json text,
            created_at text not null,
            unique(asset_id, artifact_type, path)
        );

        create table if not exists windows (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            asset_id text not null references assets(id) on delete cascade,
            start_sec real not null,
            end_sec real not null,
            duration_sec real not null,
            silence_ratio real,
            audio_active_ratio real,
            motion_score real,
            quality_score real,
            priority_score real,
            created_at text not null,
            unique(asset_id, start_sec, end_sec)
        );

        create table if not exists activity_labels (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            asset_id text not null references assets(id) on delete cascade,
            window_id text references windows(id) on delete cascade,
            start_sec real not null,
            end_sec real not null,
            label text not null,
            score real,
            source text not null,
            params_json text,
            suggested_action text,
            created_at text not null
        );

        create index if not exists idx_windows_asset_time
            on windows(asset_id, start_sec, end_sec);

        create index if not exists idx_activity_labels_asset_label
            on activity_labels(asset_id, label);

        create table if not exists scene_boundaries (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            asset_id text not null references assets(id) on delete cascade,
            time_sec real not null,
            detector text not null,
            confidence real,
            reason text,
            params_json text,
            created_at text not null
        );

        create table if not exists transcript_spans (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            asset_id text not null references assets(id) on delete cascade,
            start_sec real not null,
            end_sec real not null,
            speaker text,
            text text not null,
            kind text not null,
            confidence real,
            source text not null,
            source_run_id text,
            created_at text not null
        );

        create virtual table if not exists transcript_spans_fts
            using fts5(span_id unindexed, asset_id unindexed, text);

        create table if not exists segments (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            asset_id text not null references assets(id) on delete cascade,
            start_sec real not null,
            end_sec real not null,
            kind text not null,
            summary text not null,
            transcript_summary text,
            people_json text,
            actions_json text,
            moods_json text,
            story_roles_json text,
            quality_score real,
            usable integer not null default 1,
            source text not null,
            source_run_id text,
            created_at text not null
        );

        create virtual table if not exists segments_fts
            using fts5(segment_id unindexed, asset_id unindexed, text);

        create table if not exists observations (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            asset_id text not null references assets(id) on delete cascade,
            segment_id text references segments(id) on delete cascade,
            observation_type text not null,
            value text not null,
            confidence real,
            source text not null,
            created_at text not null
        );

        create virtual table if not exists observations_fts
            using fts5(observation_id unindexed, asset_id unindexed, observation_type unindexed, text);

        create table if not exists selects (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            segment_id text not null references segments(id) on delete cascade,
            directive_id text,
            suggested_role text,
            score real,
            trim_start_sec real,
            trim_end_sec real,
            reason text not null,
            source text not null,
            created_at text not null
        );

        create table if not exists relationships (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            from_entity_type text not null,
            from_entity_id text not null,
            to_entity_type text not null,
            to_entity_id text not null,
            relationship_type text not null,
            confidence real,
            evidence text,
            source text not null,
            created_at text not null
        );

        create table if not exists directives (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            text text not null,
            duration_sec real,
            mode text,
            created_at text not null
        );

        create table if not exists timelines (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            directive_id text references directives(id) on delete set null,
            name text not null,
            duration_target_sec real,
            timeline_json text not null,
            status text not null,
            created_at text not null
        );

        create table if not exists timeline_items (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            timeline_id text not null references timelines(id) on delete cascade,
            track_kind text not null,
            track_name text not null,
            asset_id text not null references assets(id) on delete cascade,
            segment_id text references segments(id) on delete set null,
            source_start_sec real not null,
            source_end_sec real not null,
            timeline_start_sec real not null,
            timeline_end_sec real not null,
            role text,
            reason text,
            created_at text not null
        );

        create table if not exists renders (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            timeline_id text not null references timelines(id) on delete cascade,
            path text not null,
            status text not null,
            command_json text,
            duration_sec real,
            created_at text not null
        );

        create table if not exists reviews (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            render_id text not null references renders(id) on delete cascade,
            directive text,
            review_json text not null,
            source text not null,
            created_at text not null
        );

        create table if not exists collection_summaries (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            source text not null,
            summary_json text not null,
            created_at text not null,
            updated_at text not null,
            unique(project_id, source)
        );

        create table if not exists material_bank_items (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            item_type text not null,
            name text not null,
            description text,
            evidence_json text not null,
            confidence real,
            source text not null,
            created_at text not null,
            unique(project_id, item_type, name, source)
        );

        create table if not exists editorial_context_cards (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            segment_id text not null references segments(id) on delete cascade,
            local_meaning text not null,
            corpus_meaning text not null,
            editorial_use text not null,
            avoid_pairing_notes text,
            relationship_notes text,
            warnings_json text not null,
            source text not null,
            created_at text not null,
            updated_at text not null,
            unique(segment_id, source)
        );

        create table if not exists intent_analyses (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            directive_id text references directives(id) on delete set null,
            directive_text text not null,
            analysis_json text not null,
            source text not null,
            created_at text not null
        );

        create table if not exists caption_options (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            segment_id text not null references segments(id) on delete cascade,
            context_card_id text references editorial_context_cards(id) on delete cascade,
            caption_text text not null,
            caption_type text not null,
            confidence real,
            source text not null,
            created_at text not null
        );

        create table if not exists word_alignments (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            asset_id text not null references assets(id) on delete cascade,
            span_id text references transcript_spans(id) on delete cascade,
            start_sec real not null,
            end_sec real not null,
            text text not null,
            confidence real,
            source text not null,
            created_at text not null
        );

        create index if not exists idx_word_alignments_asset_time
            on word_alignments(asset_id, start_sec, end_sec);

        create table if not exists edit_plans (
            id text primary key,
            project_id text not null references projects(id) on delete cascade,
            directive_id text references directives(id) on delete set null,
            intent_analysis_id text references intent_analyses(id) on delete set null,
            plan_json text not null,
            source text not null,
            created_at text not null
        );
        """
    )
    conn.execute(
        "insert or ignore into schema_migrations (version, applied_at) values (?, datetime('now'))",
        (SCHEMA_VERSION,),
    )
    _ensure_column(conn, "projects", "schema_version", "integer not null default 1")
    _ensure_column(conn, "timeline_items", "why_here", "text")
    _ensure_column(conn, "timeline_items", "before_context", "text")
    _ensure_column(conn, "timeline_items", "after_context", "text")
    _ensure_column(conn, "timeline_items", "caption_text", "text")
    _ensure_column(conn, "timeline_items", "transition_note", "text")
    _ensure_column(conn, "timeline_items", "continuity_score", "real")
    _ensure_column(conn, "timeline_items", "transition_json", "text")
    _ensure_column(conn, "timeline_items", "caption_decision_json", "text")
    _ensure_column(conn, "segments", "word_units_json", "text")
    _ensure_column(conn, "segments", "story_function", "text")
    _ensure_column(conn, "segments", "setup_questions_json", "text")
    _ensure_column(conn, "segments", "payoff_answers_json", "text")
    _ensure_column(conn, "segments", "audio_affordance", "text")
    _ensure_column(conn, "segments", "visual_affordance", "text")
    _ensure_column(conn, "segments", "needs_caption", "integer")
    _ensure_column(conn, "segments", "cut_notes", "text")
    _ensure_column(conn, "observations", "start_sec", "real")
    _ensure_column(conn, "observations", "end_sec", "real")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"pragma table_info({table})")}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {definition}")
