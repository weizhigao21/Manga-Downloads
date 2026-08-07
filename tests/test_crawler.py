"""src/wnacg_crawler.py 纯函数测试（不依赖网络）。"""

from lxml import html as lxml_html

from src.wnacg_crawler import (
    _extract_image_url,
    _normalize_image_url,
    _page_url,
    parse_comics,
)

# 与真实 wnacg 列表页一致的结构：ul > li.gallary_item
LIST_HTML = """
<ul>
  <li class="gallary_item">
    <div class="pic_box cate-5">
      <a href="/photos-index-page-1-aid-10001.html" title="測試漫畫">
        <img src="//img.wnacg.com/small/10001.jpg" alt="" />
      </a>
    </div>
    <div class="info">
      <div class="title">測試漫畫</div>
      <div class="meta">123張 創建於2026-08-05</div>
    </div>
  </li>
  <li class="gallary_item">
    <div class="pic_box cate-19">
      <a href="/photos-index-page-1-aid-10002.html" title="韓漫作品">
        <img src="https://img.wnacg.com/10002.jpg" alt="" />
      </a>
    </div>
    <div class="info">
      <div class="title">韓漫作品</div>
      <div class="meta">88張 創建於2026-08-04</div>
    </div>
  </li>
</ul>
"""

# 搜索结果页：<ul class="cc"> + li.li.gallary_item
SEARCH_HTML = """
<ul class="cc">
  <li class="li gallary_item">
    <div class="pic_box cate-9">
      <a href="/photos-index-page-1-aid-20001.html" title="搜索結果一">
        <img src="//img.wnacg.com/20001.jpg" alt="" />
      </a>
    </div>
    <div class="info">
      <div class="title">搜索結果一</div>
      <div class="meta">50張 創建於2026-08-01</div>
    </div>
  </li>
</ul>
"""


class TestParseComics:
    def test_parse_list_page(self):
        comics = parse_comics(LIST_HTML)
        assert len(comics) == 2

        first = comics[0]
        assert first["title"] == "測試漫畫"
        assert first["date"] == "2026-08-05"
        assert first["image_count"] == 123
        assert first["detail_url"] == "/photos-index-page-1-aid-10001.html"
        assert first["category"] == "cate-5"
        assert first["category_label"] == "同人誌"
        # 协议相对 URL 归一化为 https
        assert first["cover_url"] == "https://img.wnacg.com/small/10001.jpg"

    def test_parse_search_page(self):
        comics = parse_comics(SEARCH_HTML)
        assert len(comics) == 1
        assert comics[0]["title"] == "搜索結果一"
        assert comics[0]["category_label"] == "單行本／漢化"

    def test_parse_missing_list_node_raises(self):
        import pytest

        with pytest.raises(RuntimeError, match="未找到漫画列表节点"):
            parse_comics("<html><body><p>no items</p></body></html>")

    def test_parse_empty_li_skipped(self):
        html = "<ul><li class='gallary_item'><div>no link</div></li></ul>"
        assert parse_comics(html) == []


class TestPageUrl:
    def test_page_one_unchanged(self):
        assert _page_url("https://www.wnacg.com/albums.html", 1) == (
            "https://www.wnacg.com/albums.html"
        )

    def test_plain_list_pagination(self):
        assert _page_url("https://www.wnacg.com/albums.html", 2) == (
            "https://www.wnacg.com/albums-index-page-2.html"
        )

    def test_existing_page_replaced(self):
        assert _page_url("https://www.wnacg.com/albums-index-page-3.html", 2) == (
            "https://www.wnacg.com/albums-index-page-2.html"
        )

    def test_category_pagination(self):
        assert _page_url("https://www.wnacg.com/albums-index-cate-5.html", 2) == (
            "https://www.wnacg.com/albums-index-page-2-cate-5.html"
        )

    def test_page_zero_clamped_to_one(self):
        assert _page_url("https://www.wnacg.com/albums.html", 0) == (
            "https://www.wnacg.com/albums.html"
        )


class TestNormalizeImageUrl:
    def test_protocol_relative(self):
        assert _normalize_image_url("//img.wnacg.com/a.jpg") == (
            "https://img.wnacg.com/a.jpg"
        )

    def test_site_relative(self):
        assert _normalize_image_url("/imgs/a.jpg") == (
            "https://www.wnacg.com/imgs/a.jpg"
        )

    def test_absolute_unchanged(self):
        assert _normalize_image_url("https://img.wnacg.com/a.jpg") == (
            "https://img.wnacg.com/a.jpg"
        )


class TestExtractImageUrl:
    def test_from_pic_box(self):
        doc = lxml_html.fromstring(
            '<div class="pic_box"><span><a href="x">'
            '<img src="https://img.wnacg.com/123.jpg" /></a></span></div>'
        )
        assert _extract_image_url(doc) == "https://img.wnacg.com/123.jpg"

    def test_protocol_relative_normalized(self):
        doc = lxml_html.fromstring(
            '<div class="photo"><a href="x">'
            '<img src="//img.wnacg.com/456.jpg" /></a></div>'
        )
        assert _extract_image_url(doc) == "https://img.wnacg.com/456.jpg"

    def test_skips_icons(self):
        doc = lxml_html.fromstring(
            '<div class="pic_box"><a><img src="https://img.wnacg.com/logo.png" /></a></div>'
            '<div class="pic_box"><a><img src="https://img.wnacg.com/real.jpg" /></a></div>'
        )
        assert _extract_image_url(doc) == "https://img.wnacg.com/real.jpg"

    def test_any_anchor_image_fallback(self):
        doc = lxml_html.fromstring(
            "<html><body><div><a><img src='https://img.wnacg.com/other.jpg' /></a></div>"
            "</body></html>"
        )
        assert _extract_image_url(doc) == "https://img.wnacg.com/other.jpg"

    def test_no_image_returns_none(self):
        doc = lxml_html.fromstring("<html><body><p>nothing</p></body></html>")
        assert _extract_image_url(doc) is None
