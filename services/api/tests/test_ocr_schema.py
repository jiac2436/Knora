from sqlalchemy import inspect, text

from app import database


def test_existing_ocr_tables_gain_default_engine(tmp_path, monkeypatch):
    old_engine = database.make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with old_engine.begin() as connection:
        connection.execute(text("CREATE TABLE ocr_preferences (owner_id VARCHAR(36) PRIMARY KEY, model VARCHAR(20))"))
        connection.execute(text("CREATE TABLE ocr_jobs (id VARCHAR(36) PRIMARY KEY, model VARCHAR(20))"))
        connection.execute(text("CREATE TABLE web_read_jobs (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE web_read_preferences (owner_id VARCHAR(36) PRIMARY KEY, engine VARCHAR(20))"))
        connection.execute(text("INSERT INTO ocr_preferences (owner_id, model) VALUES ('owner', 'small')"))
        connection.execute(text("INSERT INTO ocr_jobs (id, model) VALUES ('job', 'medium')"))
        connection.execute(text("INSERT INTO web_read_jobs (id) VALUES ('web')"))
        connection.execute(text("INSERT INTO web_read_preferences (owner_id, engine) VALUES ('owner', 'crawl4ai')"))
    monkeypatch.setattr(database, "engine", old_engine)

    database.create_schema()
    database.create_schema()

    with old_engine.connect() as connection:
        assert connection.scalar(text("SELECT engine FROM ocr_preferences WHERE owner_id = 'owner'")) == "pdfium"
        assert connection.scalar(text("SELECT engine FROM ocr_jobs WHERE id = 'job'")) == "pdfium"
        assert "engine" in {column["name"] for column in inspect(connection).get_columns("ocr_jobs")}
        assert connection.scalar(text("SELECT engine FROM web_read_jobs WHERE id = 'web'")) == "legacy"
        assert connection.scalar(text("SELECT cleaning_enabled FROM web_read_jobs WHERE id = 'web'")) == 0
        assert connection.scalar(text(
            "SELECT cleaning_enabled FROM web_read_preferences WHERE owner_id = 'owner'")) == 0
    old_engine.dispose()
