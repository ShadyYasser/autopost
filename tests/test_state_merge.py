from datetime import date, datetime, timezone

from autopost.state import State, merge_states

T0 = datetime(2026, 10, 27, 8, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 27, 9, tzinfo=timezone.utc)


def test_merge_keeps_everything_both_runs_recorded(tmp_path):
    base = State(tmp_path / "base.db")
    base.insert_slot("kalima_sawaa", date(2026, 10, 27), "morning", T1, None, None, T0)
    base.record_early_delivery("takwin", "main", "v455", "facebook", "direct", "sent", "F", "u", {}, T0)
    base.close()
    import shutil
    shutil.copy(tmp_path / "base.db", tmp_path / "ours.db")
    shutil.copy(tmp_path / "base.db", tmp_path / "theirs.db")

    ours = State(tmp_path / "ours.db")            # e.g. a test-post run
    ours.record_early_delivery("kalima_sawaa", "short", "vK", "tiktok", "buffer", "sent", "T", "t", {}, T1)
    ours.record_buffer_request("tiktok_acct", T1)
    pid = ours.create_post("kalima_sawaa", "short", date(2026, 10, 27), "morning", "v1", "1.mp4", None, None, {},
                           [("facebook", "direct")], T1)
    ours.update_delivery(ours.deliveries(pid)[0].id, T1, status="sent", external_id="FB9")
    ours.set_slot_status("kalima_sawaa", date(2026, 10, 27), "morning", "done", T1)
    ours.close()

    theirs = State(tmp_path / "theirs.db")        # e.g. a cron run that only swept comments
    theirs.kv_set("fb-comment-sweep:kalima_sawaa", "x")
    theirs.record_buffer_request("youtube_acct", T0)
    theirs.close()

    merge_states(tmp_path / "theirs.db", tmp_path / "ours.db", tmp_path / "out.db")
    m = State(tmp_path / "out.db")
    platforms = {(r["brand"], r["platform"]) for r in m.early_deliveries("kalima_sawaa", "short", "vK")}
    assert platforms == {("kalima_sawaa", "tiktok")}
    assert m.early_deliveries("takwin", "main", "v455")
    assert m.kv_get("fb-comment-sweep:kalima_sawaa") == "x"
    assert m.buffer_requests_since("tiktok_acct", T0) == 1 and m.buffer_requests_since("youtube_acct", T0) == 1
    post = m.posts_for_slot("kalima_sawaa", date(2026, 10, 27), "morning")[0]
    assert m.deliveries(post["id"])[0].status == "sent"
    assert m.slots(brand="kalima_sawaa")[0].status == "done"
    assert "v1" in m.used_source_ids("kalima_sawaa", "short")


def test_merge_is_idempotent(tmp_path):
    s = State(tmp_path / "a.db")
    s.record_buffer_request("acct", T0)
    s.record_buffer_request("acct", T0)
    s.close()
    merge_states(tmp_path / "a.db", tmp_path / "a.db", tmp_path / "b.db")
    assert State(tmp_path / "b.db").buffer_requests_since("acct", T0) == 2
