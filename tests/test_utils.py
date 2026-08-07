"""src/utils.py 纯函数测试。"""

from src.utils import data_root, safe_filename


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
