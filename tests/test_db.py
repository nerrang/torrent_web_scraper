import sqlite3

import pytest

from app import db

HASH = "4e4d86cbee91db107def6d3fd24271afd28130e2"


def test_seed_and_rules(tmp_path):
    path = tmp_path / "app.db"
    db.connect(path).close()
    conn = db.connect(path)
    assert len(db.list_programs(conn)) == 17 and len(db.list_sites(conn)) == 5
    assert {p["name"] for p in db.list_programs(conn, enabled_only=True)} == {"놀면 뭐하니", "라디오스타"}
    assert len(db.list_sites(conn, enabled_only=True)) == 3
    assert db.get_setting(conn, "interval_min") == "60" and db.get_setting(conn, "doctor_model") == "sonnet"
    assert db.get_site(conn, 1)["url"] == "https://torrentsome270.com"
    assert {site["name"]: site["url"] for site in db.list_sites(conn)} == {
        name: url for name, url, _, _ in db.SEED_SITES
    }
    assert db.get_site(conn, 1)["boards"] == ["/v-4-16", "/v-2-13", "/v-4-17"]

    p = db.list_programs(conn, enabled_only=True)[0]["id"]
    assert db.add_download(conn, HASH, p, "E348.261017", "t", "s")
    assert not db.add_download(conn, HASH, p, "E999", "t", "s")
    assert not db.add_download(conn, "0" * 40, p, "E348.261017", "t", "s")
    assert db.episode_exists(conn, p, "E348.261017") and db.download_exists(conn, HASH)
    assert db.add_download(conn, "1" * 40, p, None, "t", "s")
    assert db.add_download(conn, "2" * 40, p, None, "t", "s")
    assert db.list_downloads(conn)[0]["program_name"]


def test_program_validation(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    for name, res in [("", []), ("x", ["480p"]), ("라디오스타", [])]:
        with pytest.raises(ValueError):
            db.add_program(conn, name, res)
    pid = db.add_program(conn, "x", ["1080p", "720p"])
    assert db.list_programs(conn)[-1]["resolution"] == "720p,1080p"
    db.set_program_resolution(conn, pid, [])
    assert db.list_programs(conn)[-1]["resolution"] == "any"
    with pytest.raises(ValueError):
        db.set_program_resolution(conn, pid, ["480p"])


def test_clean_resolution():
    assert db.clean_resolution([]) == "any"
    assert db.clean_resolution(["any"]) == "any"
    assert db.clean_resolution(["", " "]) == "any"
    assert db.clean_resolution(["1080p", "720p", "720p"]) == "720p,1080p"
    with pytest.raises(ValueError):
        db.clean_resolution(["480p"])


def test_migrate_old_programs_check(tmp_path):
    path = tmp_path / "app.db"
    old = sqlite3.connect(path)
    old.executescript(db.DDL.replace("CHECK (resolution <> '')", "CHECK (resolution IN ('720p','1080p','any'))"))
    old.execute("INSERT INTO programs(id, name, resolution) VALUES (7, '놀면', '720p')")
    old.execute("INSERT INTO downloads VALUES (?, 7, 'E1', 't', 's', '2026-10-18T00:00:00')", (HASH,))
    old.commit()
    old.close()
    conn = db.connect(path)
    db.set_program_resolution(conn, 7, ["720p", "1080p"])
    assert db.list_programs(conn)[0]["resolution"] == "720p,1080p"
    assert db.list_downloads(conn)[0]["program_id"] == 7
    conn.close()
    db.connect(path).close()  # second connect: no rebuild


def test_migrate_adds_site_pages(tmp_path):
    path = tmp_path / "app.db"
    old = sqlite3.connect(path)
    old.executescript(db.DDL.replace(",\n  " + db.PAGES_COL, ""))
    old.execute("INSERT INTO sites(name, url) VALUES ('a', 'https://a.com')")
    old.commit()
    old.close()
    conn = db.connect(path)
    assert db.get_site(conn, 1)["pages"] == 3
    db.update_site(conn, 1, pages=5)
    assert db.get_site(conn, 1)["pages"] == 5
    with pytest.raises(sqlite3.IntegrityError):
        db.update_site(conn, 1, pages=11)


def test_stale_marking_and_progress(tmp_path):
    path = tmp_path / "app.db"
    conn = db.connect(path)
    rid = db.create_run(conn)
    did = db.create_doctor_report(conn, 1, "sonnet")
    db.append_doctor_progress(conn, did, "one")
    db.append_doctor_progress(conn, did, "two")
    assert db.get_doctor_report(conn, did)["progress"].splitlines() == ["one", "two"]
    conn.close()
    conn = db.connect(path)
    assert db.get_run(conn, rid)["status"] == "error"
    assert db.get_doctor_report(conn, did)["status"] == "error"
    assert db.latest_doctor_report_per_site(conn)[1]["id"] == did


def test_clean_site_fields():
    assert db.clean_site_fields(" https://x.com/ ", ["/a", "", " /b "]) == ("https://x.com", ["/a", "/b"])
    for url, boards in [("https://x.com?", ["/a"]), ("https://x.com", []), ("https://x.com", ["", " "]),
                        ("https://x.com", ["/a\n/b"]), ("https://x.com", ["a"]), ("ftp://x.com", ["/a"])]:
        with pytest.raises(ValueError):
            db.clean_site_fields(url, boards)


def test_clear_download_list_keeps_deduplication(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    pid = db.list_programs(conn, enabled_only=True)[0]["id"]
    assert db.add_download(conn, HASH, pid, "E1", "first", "site")
    assert len(db.list_downloads(conn)) == 1
    db.hide_download_list(conn)
    assert db.list_downloads(conn) == []
    assert db.download_exists(conn, HASH) and db.episode_exists(conn, pid, "E1")
    assert db.count_downloads_since(conn, "2000-01-01") == 1
    assert db.add_download(conn, "2" * 40, pid, "E2", "second", "site")
    assert [d["title"] for d in db.list_downloads(conn)] == ["second"]


def test_clear_run_list_keeps_schedule_and_active_run(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    old = db.create_run(conn)
    db.finish_run(conn, old, "ok", 2, 1, "completed")
    active = db.create_run(conn)
    db.hide_run_list(conn)
    assert [r["id"] for r in db.list_runs(conn)] == [active]
    assert db.latest_run(conn)["id"] == active
    assert db.get_run(conn, old)["log"] == "completed"
    db.finish_run(conn, active, "stopped", 0, 0, "stopped")
    db.hide_run_list(conn)
    assert db.list_runs(conn) == []
    assert db.latest_run(conn)["id"] == active


def test_manual_download_reappears_after_clear(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    assert db.add_download(conn, HASH, None, None, "Sample", "직접 추가")
    db.hide_download_list(conn)
    assert db.list_downloads(conn) == []
    assert not db.add_download(conn, HASH, None, None, "Sample", "직접 추가")
    db.show_download(conn, HASH)
    assert db.list_downloads(conn)[0]["site"] == "직접 추가"


def test_restore_hidden_download_list(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    assert db.add_download(conn, HASH, None, None, "old", "직접 추가")
    db.hide_download_list(conn)
    assert db.hidden_download_count(conn) == 1
    assert db.list_downloads(conn) == []
    db.show_download_list(conn)
    assert db.hidden_download_count(conn) == 0
    assert [d["title"] for d in db.list_downloads(conn)] == ["old"]


def test_delete_download_history_removes_dedup_and_hidden_row(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    assert db.add_download(conn, HASH, None, None, "old", "직접 추가")
    db.hide_download_list(conn)
    assert db.delete_download(conn, HASH)
    assert not db.download_exists(conn, HASH)
    assert conn.execute("SELECT COUNT(*) FROM download_list_hidden").fetchone()[0] == 0
    assert not db.delete_download(conn, HASH)


def test_site_add_delete_and_no_reseed_after_all_deleted(tmp_path):
    path = tmp_path / "app.db"
    conn = db.connect(path)
    site_id = db.add_site(conn, " 새 사이트 ", "https://new.example.com/", [" /board ", ""], 2)
    site = db.get_site(conn, site_id)
    assert (site["name"], site["url"], site["boards"], site["pages"], site["enabled"]) == (
        "새 사이트", "https://new.example.com", ["/board"], 2, 0)
    report_id = db.create_doctor_report(conn, site_id, "sonnet")
    with pytest.raises(ValueError):
        db.add_site(conn, "새 사이트", "https://new.example.com", ["/board"])
    assert db.delete_site(conn, site_id)
    assert db.get_doctor_report(conn, report_id) is None
    assert not db.delete_site(conn, site_id)
    for site in db.list_sites(conn):
        assert db.delete_site(conn, site["id"])
    conn.close()
    reopened = db.connect(path)
    assert db.list_sites(reopened) == []
    reopened.close()
