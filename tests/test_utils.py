"""src/utils.py 纯函数测试。"""

from src.utils import (
    data_root,
    display_title_for_comic,
    find_existing_translation,
    safe_filename,
    split_title_segments,
)


class TestSafeFilename:
    def test_windows_illegal_chars(self):
        # \ / : * ? " < > | 全部替换为下划线
        assert safe_filename(r'a/b\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"

    def test_ampersand_half_and_full_width(self):
        assert safe_filename("A&B＆C") == "A_B_C"

    def test_fullwidth_slash_and_pipe(self):
        assert safe_filename("日／本｜漫畫") == "日_本_漫畫"

    def test_collapses_consecutive_underscores(self):
        assert safe_filename("a//b") == "a_b"

    def test_empty_returns_untitled(self):
        assert safe_filename("///") == "untitled"
        assert safe_filename("   ") == "untitled"

    def test_strips_leading_trailing(self):
        assert safe_filename("  标题  ") == "标题"

    def test_keeps_normal_name(self):
        assert safe_filename("普通标题-01") == "普通标题-01"


class TestSafeFilenameSecurity:
    """safe_filename 路径穿越与 Windows 命名规则防御。"""

    def test_dot_and_dotdot_return_untitled(self):
        assert safe_filename("..") == "untitled"
        assert safe_filename(".") == "untitled"

    def test_trailing_dots_stripped(self):
        assert safe_filename("标题.") == "标题"
        assert safe_filename("标题...") == "标题"

    def test_windows_reserved_names_prefixed(self):
        assert safe_filename("CON") == "_CON"
        assert safe_filename("con.txt") == "_con.txt"
        assert safe_filename("NUL") == "_NUL"
        assert safe_filename("COM1") == "_COM1"
        assert safe_filename("LPT9") == "_LPT9"

    def test_control_chars_replaced(self):
        assert safe_filename("a\x00b") == "a_b"
        assert safe_filename("a\x1fb") == "a_b"

    def test_dotdot_as_plain_text_kept(self):
        # 中间的 ".." 只是普通文本（不构成路径段），正常保留
        assert safe_filename("a..b") == "a..b"

    def test_no_path_traversal_segments(self):
        # 拼接场景：safe_filename 结果绝不能是 "." / ".." / 含分隔符
        result = safe_filename("..\\..\\evil")
        assert "\\" not in result
        assert "/" not in result
        assert result not in (".", "..")


class TestDataRoot:
    def test_points_to_project_root(self):
        root = data_root()
        assert (root / "src").is_dir()
        assert (root / "Config").is_dir()


class TestSplitTitleSegments:
    def test_typical_wnacg_title(self):
        segments = split_title_segments(
            "[钢鉄しゃぼん玉 (玉ぼん)] 続·カノジョは… [惑星]"
        )
        assert segments == [
            ("[钢鉄しゃぼん玉 (玉ぼん)]", False),
            (" 続·カノジョは… ", True),
            ("[惑星]", False),
        ]

    def test_plain_title_all_translatable(self):
        assert split_title_segments("普通标题") == [("普通标题", True)]

    def test_multiple_brackets_preserved(self):
        segments = split_title_segments("[A] [B] 主体 [C]")
        preserved = [text for text, tr in segments if not tr]
        assert preserved == ["[A]", "[B]", "[C]"]
        # 仅空白片段不参与翻译，其余主体文本保留原样（含首尾空格）
        translatable = [text for text, tr in segments if tr and text.strip()]
        assert translatable == [" 主体 "]

    def test_brackets_only(self):
        assert split_title_segments("[A][B]") == [("[A]", False), ("[B]", False)]

    def test_empty_title(self):
        assert split_title_segments("") == []
        assert split_title_segments(None) == []

    def test_inner_parenthesis_kept_in_bracket(self):
        # 方括号内的括号不影响整块保留
        segments = split_title_segments("[社团 (作者)] 标题")
        assert segments[0] == ("[社团 (作者)]", False)


class TestFindExistingTranslation:
    def test_pipe_separated_returns_chinese_part(self):
        # 「日文原名｜中文翻译」取中文段
        assert find_existing_translation(
            "破魔の巫女 淫狱に堕つ 2｜破魔巫女 堕入淫狱 2"
        ) == "破魔巫女 堕入淫狱 2"

    def test_pipe_separated_english_not_used(self):
        # 「日文｜English」候选不含汉字，交给 AI 翻译
        assert find_existing_translation("日本語タイトル｜English Title") is None

    def test_reversed_order_handled(self):
        # 「中文｜日文」反了也能取到中文段
        assert find_existing_translation("中文标题｜日本語タイトル") == "中文标题"

    def test_already_chinese_no_call(self):
        # 本身就是中文（无假名、含汉字）：直接使用
        assert find_existing_translation("破魔巫女 堕入淫狱 2") == "破魔巫女 堕入淫狱 2"

    def test_japanese_no_pipe_returns_none(self):
        assert find_existing_translation("オリジナルタイトル") is None

    def test_empty_or_blank(self):
        assert find_existing_translation("") is None
        assert find_existing_translation("  ") is None


class TestDisplayTitleForComic:
    def test_translated_title_priority(self):
        assert display_title_for_comic("オリジナル", "原创标题") == "原创标题"

    def test_builtin_chinese_translation(self):
        assert display_title_for_comic(
            "破魔の巫女 淫狱に堕つ 2｜破魔巫女 堕入淫狱 2"
        ) == "破魔巫女 堕入淫狱 2"

    def test_fallback_to_original(self):
        assert display_title_for_comic("[A] オリジナル [B]") == "[A] オリジナル [B]"

    def test_empty(self):
        assert display_title_for_comic("", "") == ""
