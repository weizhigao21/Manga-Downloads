"""src/utils.py 纯函数测试。"""

from src.utils import data_root, safe_filename, split_title_segments


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
