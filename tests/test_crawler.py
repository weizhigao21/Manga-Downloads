"""src/wnacg_crawler.py 纯函数测试（不依赖网络）。"""

from lxml import html as lxml_html

from src.wnacg_crawler import (
    _extract_image_url,
    _normalize_image_url,
    _page_url,
    parse_collection_chapters,
    parse_comics,
)


def test_parse_collection_chapters_skips_start_link_and_keeps_page_counts():
    page = """<a id="reader-btn" href="/photos-slide-aid-258605-sid-390979.html">开始阅读</a>
    <div class="sr_compact">
      <a class="tagshow" href="/photos-slide-aid-258605-sid-390979.html"
         title="第1話《第一章》 33P 2024-07-17">第1話 第一章</a>
      <a class="tagshow" href="/photos-slide-aid-366617-sid-390979.html"
         title="第2話《第二章》 42P 2026-06-23">第2話 第二章</a>
    </div>"""
    assert parse_collection_chapters(page) == [
        {"title": "第1話 第一章", "detail_url": "/photos-index-aid-258605.html", "image_count": 33},
        {"title": "第2話 第二章", "detail_url": "/photos-index-aid-366617.html", "image_count": 42},
    ]


def test_explicit_picture_page_range_can_exceed_link_page_number(monkeypatch):
    """page-1 阅读链接不应裁掉显式请求的第二个列表页。"""
    import src.wnacg_crawler as crawler

    requested = []

    def _fake_fetch(url, headers):
        requested.append(url)
        return """<div id="bodywrap"><ul><li class="gallary_item">
            <div class="pic_box"><a href="/photos-view-id-25.html">图片</a></div>
            <div class="info"><span class="name">25</span></div>
        </li></ul></div>"""

    monkeypatch.setattr(crawler, "fetch_with_retry", _fake_fetch)
    monkeypatch.setattr(crawler, "_fetch_picture_link", lambda link: "http://cdn/25.jpg")
    pictures = crawler.parse_picture_page(
        "/photos-index-page-1-aid-42.html", start=2, end=2
    )
    assert requested == ["https://www.wnacg.com/photos-index-page-2-aid-42.html"]
    assert pictures == [{"link": "http://cdn/25.jpg", "number": "25"}]


def test_list_page_only_does_not_fetch_every_picture_detail(monkeypatch):
    """阅读器只取列表条目时，不逐张请求大图直链。"""
    import src.wnacg_crawler as crawler

    page_html = "<div id='bodywrap'><ul>" + "".join(
        f"<li class='gallary_item'><div class='pic_box'><a href='/view-{i}.html'>图</a></div>"
        f"<div class='info'><span class='name'>{i}</span></div></li>"
        for i in range(1, 25)
    ) + "</ul></div>"
    monkeypatch.setattr(crawler, "fetch_with_retry", lambda url, headers: page_html)
    detail_calls = []
    monkeypatch.setattr(
        crawler, "_fetch_picture_link", lambda link: detail_calls.append(link)
    )
    entries = crawler.parse_picture_page(
        "/photos-index-page-1-aid-42.html", start=1, end=1, resolve_links=False
    )
    assert len(entries) == 24
    assert entries[0] == {"detail_link": "/view-1.html", "number": "1"}
    assert detail_calls == []

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
    def test_search_highlight_tags_are_removed_from_title(self):
        page = """<ul><li class="gallary_item">
          <div class="pic_box cate-5"><a href="/photos-index-aid-7.html"
          title="[&lt;em&gt;殿様&lt;/em&gt;&lt;em&gt;ペン&lt;/em&gt;&lt;em&gt;ギン&lt;/em&gt;] 作品★"></a></div>
          <div class="info"><div class="meta">12張</div></div>
        </li></ul>"""
        assert parse_comics(page)[0]["title"] == "[殿様ペンギン] 作品★"

    def test_collection_metadata_is_chapters_not_zero_images(self):
        html = """<ul><li class="gallary_item">
          <div class="pic_box cate-1"><a href="/photos-index-aid-390979.html"
          title="合集作品"><img src="//img.wnacg.com/390979.jpg"></a></div>
          <div class="info"><div class="title">合集作品</div>
          <div class="meta"><span>2話</span>， 更新於2026-10-01</div></div>
        </li></ul>"""
        comic = parse_comics(html)[0]
        assert comic["image_count"] == 0
        assert comic["chapter_count"] == 2
        assert comic["date"] == "2026-10-01"

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


class TestCategorySearch:
    def test_selected_category_filters_search_results(self, monkeypatch):
        import src.wnacg_crawler as crawler

        categories = ("cate-37", "cate-1", "cate-9", "cate-38")
        mixed_html = "<ul class='cc'>" + "".join(
            f"<li class='li gallary_item'><div class='pic_box {category}'>"
            f"<a href='/photos-index-aid-{i}.html' title='作品{i}'>"
            f"<img src='//example.com/{i}.jpg'></a></div></li>"
            for i, category in enumerate(categories, 1)
        ) + "</ul>"
        requested = []
        monkeypatch.setattr(
            crawler, "fetch_with_retry",
            lambda url, headers: requested.append(url) or mixed_html,
        )

        assert [c["category"] for c in crawler.search_comics("测试", category="cate-37")] == [
            "cate-37"
        ]
        assert [c["category"] for c in crawler.search_comics("测试", category="cate-5")] == [
            "cate-37", "cate-1"
        ]
        assert [c["category"] for c in crawler.search_comics("测试", category="cate-19")] == [
            "cate-38"
        ]
        assert [c["category"] for c in crawler.search_comics("测试")] == list(categories)
        assert all("f=_all" in url for url in requested)


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
