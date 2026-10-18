"""The provisioning steps a setup run takes against SereneDB.

Offline: every test drives :mod:`backend.db.provision` against a fake cursor and
asserts on the SQL it issues, because the three things that can go wrong here
are a leaked key, a secret left ambiguous, and a corpus needlessly rebuilt.
"""

import psycopg2
import pytest

from backend.db import provision
from sql_render import render
from backend.db.config import EMBEDDING_SECRET


class FakeCursor:
    """Records executed SQL and answers the queries provisioning asks."""

    def __init__(self, secrets=None, tables=None, rows=None, embed_dimension=3072):
        self.executed: list[str] = []
        self.secrets = secrets if secrets is not None else []
        self.tables = tables if tables is not None else set()
        self.rows = rows or {}
        self.embed_dimension = embed_dimension
        self._result = None

    def execute(self, statement, params=None):
        text = render(statement) if hasattr(statement, "as_string") else str(statement)
        self.executed.append(text)
        lowered = text.lower()
        if "sdb_secrets()" in lowered:
            name = text.split("'")[-2] if "'" in text else None
            match = [s for s in self.secrets if s["name"] == name]
            self._result = match[0] if match else None
        elif "information_schema.tables" in lowered:
            name = text.split("'")[-2]
            self._result = {"n": 1 if name in self.tables else 0}
        elif "ai_embed" in lowered:
            self._result = {"dimension": self.embed_dimension}
        elif "count(embedding)" in lowered:
            table = text.split('"')[1]
            self._result = {
                "n": self.rows.get(table, 0),
                "embedded": self.rows.get(f"{table}:embedded", 0),
            }
        elif "count(*)" in lowered:
            table = text.split('"')[1] if '"' in text else ""
            self._result = {"n": self.rows.get(table, 0)}
        elif "current_setting" in lowered:
            self._result = {"m": "488.2 MiB"}
        else:
            self._result = {"ok": 1}

    def fetchone(self):
        return self._result

    @property
    def sql(self) -> str:
        return "\n".join(self.executed)


# --- the embedding secret --------------------------------------------------


def test_the_secret_is_created_persistent_so_it_survives_a_restart():
    cur = FakeCursor(secrets=[{"name": EMBEDDING_SECRET, "type": "openai",
                               "provider": "config", "persistent": True}])
    state = provision.ensure_embedding_secret("a-key", cur=cur)
    assert "CREATE OR REPLACE PERSISTENT SECRET" in cur.sql
    assert state["persistent"] is True


def test_the_other_storage_backend_is_dropped_first():
    """A temporary secret of the same name makes every ai_embed call ambiguous."""
    cur = FakeCursor()
    provision.ensure_embedding_secret("a-key", cur=cur)
    drop_at = cur.sql.index("DROP TEMPORARY SECRET IF EXISTS")
    create_at = cur.sql.index("CREATE OR REPLACE PERSISTENT SECRET")
    assert drop_at < create_at, "the shadowing secret must go before the new one"


def test_a_temporary_secret_drops_the_persistent_one_instead():
    cur = FakeCursor()
    provision.ensure_embedding_secret("a-key", persistent=False, cur=cur)
    assert "DROP PERSISTENT SECRET IF EXISTS" in cur.sql
    assert "CREATE OR REPLACE SECRET" in cur.sql
    assert "PERSISTENT SECRET \"gemini\"" not in cur.sql.split("DROP")[-1]


def test_the_secret_names_the_openai_type_and_the_embeddings_path():
    cur = FakeCursor()
    provision.ensure_embedding_secret("a-key", cur=cur)
    assert "TYPE openai" in cur.sql
    assert provision.GEMINI_BASE_URL in cur.sql
    assert provision.EMBEDDINGS_PATH in cur.sql


def test_a_missing_key_is_refused_before_any_sql_runs():
    cur = FakeCursor()
    with pytest.raises(ValueError, match="GOOGLE_API_KEY"):
        provision.ensure_embedding_secret("", cur=cur)
    assert cur.executed == []


def test_the_key_never_reaches_the_log_or_the_returned_state(caplog):
    cur = FakeCursor()
    with caplog.at_level("DEBUG"):
        state = provision.ensure_embedding_secret("super-secret-value", cur=cur)
    assert "super-secret-value" not in caplog.text
    assert "super-secret-value" not in repr(state)


# --- waiting ---------------------------------------------------------------


def test_waiting_returns_as_soon_as_the_server_answers(monkeypatch):
    monkeypatch.setattr(provision, "server_is_up", lambda: True)
    waited = provision.wait_for_server(timeout_s=5, sleep=lambda s: None)
    assert waited >= 0


def test_waiting_gives_up_and_names_the_target(monkeypatch):
    monkeypatch.setattr(provision, "server_is_up", lambda: False)
    clock = iter([0.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
    with pytest.raises(TimeoutError, match="SERENEDB_PORT"):
        provision.wait_for_server(
            timeout_s=3, interval_s=1, sleep=lambda s: None, now=lambda: next(clock)
        )


def test_readiness_runs_a_query_not_just_a_connect(monkeypatch):
    """A starting server accepts the socket and then fails the first statement."""
    attempted = {"query": False}

    class Conn:
        def cursor(self, **kwargs):
            attempted["query"] = True
            raise psycopg2.OperationalError("still starting up")

        def close(self):
            pass

    monkeypatch.setattr(provision.psycopg2, "connect", lambda **kw: Conn())
    assert provision.server_is_up() is False
    assert attempted["query"], "readiness must run a statement"


# --- what needs loading ----------------------------------------------------


def test_a_loaded_corpus_is_not_reloaded(monkeypatch):
    monkeypatch.setattr(
        provision,
        "corpus_state",
        lambda cur=None: {
            "openstack": {"loaded": True, "rows": 207820, "expected_rows": 207820},
            "bgl": {"loaded": False, "rows": 0},
        },
    )
    assert provision.pending_datasets(["openstack", "bgl"]) == ["bgl"]


def test_an_empty_table_still_counts_as_pending(monkeypatch):
    monkeypatch.setattr(
        provision,
        "corpus_state",
        lambda cur=None: {"openstack": {"loaded": True, "rows": 0}},
    )
    assert provision.pending_datasets(["openstack"]) == ["openstack"]


def test_strict_rows_reloads_a_corpus_that_is_short(monkeypatch):
    monkeypatch.setattr(
        provision,
        "corpus_state",
        lambda cur=None: {
            "openstack": {"loaded": True, "rows": 2000, "expected_rows": 207820}
        },
    )
    assert provision.pending_datasets(["openstack"]) == []
    assert provision.pending_datasets(["openstack"], strict_rows=True) == ["openstack"]


def test_a_sample_loader_id_is_judged_by_the_table_it_loads(monkeypatch):
    monkeypatch.setattr(
        provision,
        "corpus_state",
        lambda cur=None: {"bgl": {"loaded": True, "rows": 4747963}},
    )
    assert provision.pending_datasets(["bgl_2k"]) == []


# --- the report ------------------------------------------------------------


def test_the_report_says_when_a_secret_would_not_survive_a_restart():
    state = {
        "server": "localhost:7890/postgres",
        "memory_limit": "488.2 MiB",
        "memory_budget": "512MB",
        "secret": {"name": "gemini", "type": "openai", "persistent": False},
        "secret_name": "gemini",
        "embedding_model": "gemini-embedding-2",
        "configured_dimension": 3072,
        "corpora": {"team_ownership": {"loaded": False}},
    }
    assert "TEMPORARY" in provision.format_report(state)


def test_the_report_calls_out_a_missing_secret():
    state = {
        "server": "localhost:7890/postgres",
        "memory_limit": "488.2 MiB",
        "memory_budget": "512MB",
        "secret": None,
        "secret_name": "gemini",
        "embedding_model": "gemini-embedding-2",
        "configured_dimension": 3072,
        "corpora": {"team_ownership": {"loaded": False}},
    }
    rendered = provision.format_report(state)
    assert "MISSING" in rendered and "ai_embed cannot run" in rendered


def test_the_report_flags_a_stored_width_the_model_no_longer_returns():
    state = {
        "server": "localhost:7890/postgres",
        "memory_limit": "488.2 MiB",
        "memory_budget": "512MB",
        "secret": {"name": "gemini", "type": "openai", "persistent": True},
        "secret_name": "gemini",
        "embedding_model": "gemini-embedding-2",
        "configured_dimension": 3072,
        "embedding": {
            "can_embed": True,
            "model_dimension": 3072,
            "configured_dimension": 768,
            "dimension_matches_config": False,
        },
        "corpora": {
            "bgl": {
                "loaded": True,
                "rows": 4747963,
                "templates": 20352,
                "embedded": 20352,
                "stored_dimension": 768,
            },
            "team_ownership": {"loaded": True, "rows": 39},
        },
    }
    rendered = provision.format_report(state)
    assert "DISAGREES" in rendered
    assert "768 dimensions" in rendered


def test_the_embedding_probe_reports_the_failure_instead_of_raising():
    class Failing(FakeCursor):
        def execute(self, statement, params=None):
            text = render(statement) if hasattr(statement, "as_string") else str(statement)
            if "ai_embed" in text:
                raise psycopg2.errors.ExternalRoutineException(
                    "ai_embed: secret 'gemini' not found"
                )
            super().execute(statement, params)

    probe = provision.embedding_probe(cur=Failing())
    assert probe["can_embed"] is False
    assert "not found" in probe["error"]
