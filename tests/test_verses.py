"""Verse cards: only a close word-for-word reading of a real verse gets a card."""
from autopost.edits import EditPlan, Segment
from autopost.frame import FrameStyle, frame_filter, input_order
from autopost.verses import Bible, Verse, VerseCard, find_quotes, normalize, reference


def bible(*rows):
    return Bible([Verse(b, c, n, t, tuple(normalize(t))) for b, c, n, t in rows])


JOHN_13_23 = "وَكَانَ مُتَّكِئًا فِي حِضْنِ يَسُوعَ وَاحِدٌ مِنْ تَلَامِيذِهِ، كَانَ يَسُوعُ يُحِبُّهُ."
PS_22_6 = "أَمَّا أَنَا فَدُودَةٌ لَا إِنْسَانٌ. عَارٌ عِنْدَ ٱلْبَشَرِ وَمُحْتَقَرُ ٱلشَّعْبِ."
B = bible((43, 13, 23, JOHN_13_23), (43, 13, 24, "فَأَوْمَأَ إِلَيْهِ سِمْعَانُ بُطْرُسُ أَنْ يَسْأَلَ مَنْ عَسَى أَنْ يَكُونَ ٱلَّذِي قَالَ عَنْهُ."),
          (19, 22, 6, PS_22_6))


def test_normalize_drops_diacritics_and_unifies_letters():
    assert normalize("ٱللهُ أَحَبَّ ٱلْعَالَمَ.") == ["الله", "احب", "العالم"]


def test_reference_uses_arabic_digits_and_singular_psalm():
    assert reference(40, 26, 26) == "متى ٢٦: ٢٦"
    assert reference(19, 23, 1, 3) == "المزمور ٢٣: ١-٣"


def test_verse_read_out_gets_a_card_with_the_printed_text():
    segs = [Segment(0.0, 3.0, "زي ما النص الكتابي بيقول في انجيل يحنى 13 23"),
            Segment(3.0, 8.0, "وكان متكئا في حضن يسوع واحد من تلاميذه كان يسوع يحبه"),
            Segment(8.0, 10.0, "فاتكأ ذاك على صدر يسوع")]
    cards = find_quotes(segs, B)
    assert len(cards) == 1
    c = cards[0]
    assert c.text == JOHN_13_23 and c.reference == "يوحنا ١٣: ٢٣"
    assert 2.5 <= c.start <= 3.5 and 7.5 <= c.end <= 9.0


def test_whisper_slips_are_tolerated_but_a_paraphrase_is_not():
    slip = [Segment(10.0, 15.0, "أما أنا فدودة لا إنسان عار عند البشر وحقر وسط الشعب")]
    assert [c.reference for c in find_quotes(slip, B)] == ["المزمور ٢٢: ٦"]
    paraphrase = [Segment(0.0, 5.0, "هو بيقول عن نفسه انه دودة مش انسان والناس بتحتقره")]
    assert find_quotes(paraphrase, B) == []


def test_short_fragment_gets_no_card():
    assert find_quotes([Segment(0.0, 2.0, "كان يسوع يحبه")], B) == []


def test_identical_text_in_two_places_gets_no_card():
    twin = bible((43, 13, 23, JOHN_13_23), (43, 21, 20, JOHN_13_23))
    segs = [Segment(0.0, 5.0, "وكان متكئا في حضن يسوع واحد من تلاميذه كان يسوع يحبه")]
    assert find_quotes(segs, twin) == []


def test_card_round_trips_through_json():
    c = VerseCard(1.0, 4.0, PS_22_6, "المزمور ٢٢: ٦", 0.9)
    assert VerseCard.from_json(c.to_json()) == c


def test_cards_blur_their_own_stretch_and_follow_the_cold_open():
    cards = [VerseCard(2.0, 6.0, PS_22_6, "المزمور ٢٢: ٦")]
    plan = EditPlan(hook=(10.0, 12.0), cards=cards)
    idx = input_order(plan)
    assert idx["card0"] == max(idx.values())                   # card images are the last inputs
    graph = frame_filter(FrameStyle(), (0, 0, 720, 1280), plan, audio=True)
    assert "between(t,4.000,8.000)" in graph                   # shifted by the 2 s cold open
    assert "trim" not in graph                                 # nothing is buffered until the card
    assert "gblur" in graph and f"[{idx['card0']}:v]" in graph and "[body]" in graph


# ---------------------------------------------------------------- on-screen cards and the model check

from autopost.verses import book_candidates, cards_from_screen, combine, screen_refs, speech_candidates

JOHN_13_25 = "فَٱتَّكَأَ ذَاكَ عَلَى صَدْرِ يَسُوعَ وَقَالَ لَهُ: «يَا سَيِّدُ، مَنْ هُوَ؟»."
B2 = bible((43, 13, 23, JOHN_13_23), (43, 13, 25, JOHN_13_25), (6, 13, 25, "وَكَانَ تُخُمُ يَعْزِيرَ وَكُلُّ مُدُنِ جِلْعَادَ."))


def test_abbreviations_and_ocr_slips():
    assert book_candidates("يو") == [43]
    assert 43 in book_candidates("يى") and len(book_candidates("يى")) > 1     # a slip: the text decides
    assert book_candidates("1كو") == [46]


def test_screen_reference_offers_both_digit_orders():
    hits = screen_refs("‏(مر21:3)‎")
    assert {(h.chapter, h.first) for h in hits} == {(21, 3), (3, 21)}


def test_creator_card_on_screen_gives_our_card_at_the_same_time():
    frames = [(88.0, "ظ (يو 13: 23)"), (89.0, '"فاتكأ ذاك على صدر يسوع وقال له: «يا سيد من هو؟" (يى 13: 25)'),
              (90.0, "ظ (يد 13: 25) فاتكأ ذاك على صدر"), (91.0, "(يى 13: 25)")]
    cards = cards_from_screen(frames, [], B2)
    assert [(c.reference, c.start, c.source) for c in cards] == [("يوحنا ١٣: ٢٥", 89.0, "screen")]
    assert cards[0].text == JOHN_13_25 and cards[0].end == 92.0


def test_reference_on_screen_without_matching_words_gets_no_card():
    frames = [(5.0, "(يو 13: 25) كلام تاني خالص"), (6.0, "(يو 13: 25) كلام تاني خالص")]
    assert cards_from_screen(frames, [], B2) == []


def test_spoken_reference_with_the_verse_said_is_a_candidate():
    segs = [Segment(0.0, 3.0, "زي ما بيقول في انجيل يوحنا 13 25"),
            Segment(3.0, 6.0, "فاتكأ ذاك على صدر يسوع وقال له")]
    assert [c.reference for c in speech_candidates(segs, B2)] == ["يوحنا ١٣: ٢٥"]


def test_screen_cards_win_over_spoken_ones():
    s = VerseCard(10.0, 14.0, JOHN_13_25, "يوحنا ١٣: ٢٥", 1.0, "screen")
    clash = VerseCard(12.0, 16.0, JOHN_13_23, "يوحنا ١٣: ٢٣", 1.0, "speech")
    again = VerseCard(20.0, 24.0, JOHN_13_25, "يوحنا ١٣: ٢٥", 1.0, "speech")
    later = VerseCard(40.0, 44.0, JOHN_13_23, "يوحنا ١٣: ٢٣", 1.0, "speech")
    assert combine([s], [clash, again, later]) == [s, later]


def test_back_to_back_cards_share_one_blur():
    cards = [VerseCard(2.0, 5.0, PS_22_6, "المزمور ٢٢: ٦"), VerseCard(5.0, 8.0, PS_22_6, "المزمور ٢٢: ٦")]
    graph = frame_filter(FrameStyle(), (0, 0, 720, 1280), EditPlan(cards=cards), audio=True)
    assert graph.count("[bl") == 2 and "between(t,2.000,8.000)" in graph      # one blur span, two cards


class _Guard:
    def __init__(self, keep):
        self.keep, self.calls = keep, []

    def confirm_verses(self, transcript, candidates):
        self.calls.append(candidates)
        return self.keep


def test_model_parses_keep_list():
    from autopost.title_guard import TitleGuard

    class Backend:
        def complete(self, system, user, max_tokens=500):
            assert "verse" in system
            return '{"keep": [2, 7], "reason": "x"}'
    g = TitleGuard(context="", sleep=lambda s: None, backend=Backend())
    assert g.confirm_verses("نص", [{"reference": "a"}, {"reference": "b"}]) == [1]


def test_drive_source_keeps_screen_cards_and_only_confirmed_speech(app, tmp_path, monkeypatch):
    from pathlib import Path
    from autopost import verses
    from autopost.content.base import VideoItem
    from autopost.content.drive_source import DriveContent
    from autopost.state import State

    dc = DriveContent(app, State(tmp_path / "s.db"), None, tmp_path / "w", False)
    v = VideoItem(id="v1", name="reel.mp4", created=None)
    dc._stream_of["v1"] = ("kalima_sawaa", "short")
    segs = [Segment(1.0, 4.0, "وكان متكئا في حضن يسوع واحد من تلاميذه كان يسوع يحبه")]
    monkeypatch.setattr(dc, "cached_segments", lambda b, vid: segs)
    monkeypatch.setattr(dc, "transcript", lambda b, vid: segs[0].text)
    monkeypatch.setattr(verses, "load_bible", lambda: B2)
    dc._screen["v1"] = [(30.0, "(يو 13: 25) فاتكأ ذاك على صدر يسوع"), (31.0, "(يو 13: 25)")]
    guard = _Guard(keep=[])                                    # the model drops the spoken one
    monkeypatch.setattr(dc, "guard_for", lambda b: guard)
    cards = dc.verse_cards("kalima_sawaa", "short", v, 60.0)
    assert [(c.reference, c.source) for c in cards] == [("يوحنا ١٣: ٢٥", "screen")]
    assert guard.calls and guard.calls[0][0]["reference"] == "يوحنا ١٣: ٢٣"
    assert dc.verse_cards("kalima_sawaa", "own", v, 60.0) == []                 # not on the own folder
    dc._screen.clear()                                          # cached in state: no OCR, no model again
    assert [c.reference for c in dc.verse_cards("kalima_sawaa", "short", v, 60.0)] == ["يوحنا ١٣: ٢٥"]
    assert len(guard.calls) == 1
