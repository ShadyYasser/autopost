from autopost.config import TemplateConfig
from autopost.content.text import (
    limit_field,
    render,
    title_from_filename,
    trim_sentences,
    utf8_len,
    utf16_len,
)

# Real row 2 of the Takwin sheet.
CAPTION = ("يا هادي القلوب، أسألك بفضلك ورحمتك أن تهدي من أحب إلى الإسلام، وأن تيسر لها سبل الهدى، "
           "وتفتح بصيرتها لمعرفة الحق، وتجعل الإيمان نورا في قلبها وسكينة في روحها، ولا تحرمها لذة القرب منك. "
           "اللهم صل وسلم وبارك على نبينا محمد....")
TITLE = "يا هادي، اهد من أحب للإسلام ويسر لها سبل الهدى، وصل وسلم على محمد"


def test_real_takwin_row_fits_everywhere_untouched():
    t = TemplateConfig
    v = {"caption": CAPTION, "title": TITLE}
    assert render(t(text="{caption}"), "instagram", v)["text"] == CAPTION
    yt = render(t(title="{title}", description="{caption}"), "youtube", v)
    assert yt == {"title": TITLE, "description": CAPTION}
    pin = render(t(title="{title}", description="{caption}"), "pinterest", v)
    assert pin["description"] == CAPTION and pin["title"] == TITLE


def test_pinterest_trims_at_last_full_sentence():
    sentence = "يا رب اهد قلبها إلى الإسلام وارزقها السكينة والطمأنينة في كل حين. "
    text = sentence * 20                                   # ~1360 chars
    out = limit_field("pinterest", "description", text)
    assert utf16_len(out) <= 800
    assert out.endswith(".")
    assert text.startswith(out)
    assert utf16_len(out) > 800 - len(sentence)            # kept as many whole sentences as fit


def test_arabic_question_mark_and_newline_are_boundaries():
    text = ("سؤال مهم جدا؟ " * 30) + "\n" + ("كلام بلا نهاية " * 100)
    out = trim_sentences(text, 500)
    assert out.endswith("؟") and utf16_len(out) <= 500
    text2 = ("سطر أول بدون نقطة " * 10).strip() + "\n" + ("سطر ثان طويل " * 100)
    out2 = trim_sentences(text2, 400)
    assert "\n" not in out2 and out2 == ("سطر أول بدون نقطة " * 10).strip()


def test_no_sentence_boundary_falls_back_to_words_with_ellipsis():
    out = trim_sentences("كلمة " * 300, 100)
    assert out.endswith("…") and utf16_len(out) <= 100
    assert not out[:-1].endswith(" ")


def test_decimal_point_is_not_a_sentence_end():
    text = "النسخة 3.5 من الشرح " + "تفاصيل كثيرة " * 80
    out = trim_sentences(text, 60)
    assert out.endswith("…")


def test_never_cuts_inside_a_quranic_verse():
    verse = "﴿ وَمَن يُؤْمِن بِاللَّهِ يَهْدِ قَلْبَهُ. وَاللَّهُ بِكُلِّ شَيْءٍ عَلِيمٌ ﴾"
    text = "مقدمة قصيرة. " + verse + " " + "شرح طويل " * 100
    out = trim_sentences(text, 60)
    assert out == "مقدمة قصيرة."


def test_youtube_description_limit_is_in_bytes():
    text = ("دعاء جميل ومؤثر للهداية. " * 300)              # ~7800 chars, ~14000 bytes
    out = limit_field("youtube", "description", text)
    assert utf8_len(out) <= 5000 and out.endswith(".")


def test_youtube_title_cleaned_and_cut_at_word():
    title = "<عنوان> " + "كلمة طويلة " * 20 + "😅"
    out = limit_field("youtube", "title", title)
    assert "<" not in out and ">" not in out
    assert utf16_len(out) <= 100 and not out.endswith(" ")
    assert title.replace("<", "").replace(">", "").startswith(out)


def test_emoji_count_double_for_title_limit():
    title = "😅" * 60                                      # 60 code points, 120 UTF-16 units
    assert utf16_len(limit_field("pinterest", "title", title)) <= 100


def test_limit_overrides_from_settings():
    out = limit_field("instagram", "text", "جملة قصيرة. " * 50, {"instagram": {"text": 100}})
    assert utf16_len(out) <= 100


def test_title_from_filename():
    assert title_from_filename("مسيحي شاف صدمة عمره بعينه! 😅 دخل واثق من نفسه ثم اصطدم بحقيقة الشـ.رك الواضح!!.mp4") == \
        "مسيحي شاف صدمة عمره بعينه! 😅 دخل واثق من نفسه ثم اصطدم بحقيقة الشـ.رك الواضح!!"
    assert title_from_filename("Clip.MOV") == "Clip"
    assert title_from_filename("a.b.c.webm") == "a.b.c"
    assert title_from_filename(
        "Copy of خادم أرثوذكسي： العيش هو رب الكون!!.mp4",
        strip_prefixes=["Copy of "], replacements={"：": ":"},
    ) == "خادم أرثوذكسي: العيش هو رب الكون!!"


def test_kalima_long_title_capped_at_100():
    name = ("Copy of الكنائس بتتخانق مع بعض بسبب تحريفهم للكتاب المقدس-انت اللى حرفت كتابنا!"
            "مراجع مسيحية مؤلمة لكل مسيحي!.mp4")
    title = title_from_filename(name, ["Copy of "])
    v = {"title": title}
    yt = render(TemplateConfig(title="{title}", description="{title}"), "youtube", v)
    assert utf16_len(yt["title"]) <= 100 and yt["description"] == title
    fb = render(TemplateConfig(text="{title}"), "facebook", v)
    assert fb["text"] == title
