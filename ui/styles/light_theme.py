"""浅色现代主题样式定义。"""

PALETTE = {
    "background": "#F5F7FA",
    "surface": "#FFFFFF",
    "surface_hover": "#F0F2F5",
    "primary": "#3B82F6",
    "primary_hover": "#2563EB",
    "text": "#1F2937",
    "text_secondary": "#6B7280",
    "border": "#E5E7EB",
    "shadow": "rgba(0, 0, 0, 0.08)",
    "success": "#10B981",
    "error": "#EF4444",
}

LIGHT_THEME_QSS = f"""
QMainWindow {{
    background-color: {PALETTE["background"]};
}}

QWidget {{
    font-family: "Microsoft YaHei UI", "PingFang SC", "Segoe UI", sans-serif;
    color: {PALETTE["text"]};
}}

/* 顶部工具栏 */
QFrame#toolbar {{
    background-color: {PALETTE["surface"]};
    border-bottom: 1px solid {PALETTE["border"]};
}}

/* 底部翻页栏 */
QFrame#pageBar {{
    background-color: {PALETTE["surface"]};
    border-top: 1px solid {PALETTE["border"]};
}}

QLabel#pageLabel {{
    font-size: 14px;
    font-weight: 600;
    color: {PALETTE["text"]};
}}

QPushButton#pageButton {{
    background-color: {PALETTE["surface"]};
    color: {PALETTE["text"]};
    border: 1px solid {PALETTE["border"]};
}}

QPushButton#pageButton:hover {{
    background-color: {PALETTE["surface_hover"]};
}}

QPushButton#pageButton:disabled {{
    color: {PALETTE["text_secondary"]};
    background-color: {PALETTE["surface"]};
    border: 1px solid {PALETTE["border"]};
}}

QPushButton#downloadButton {{
    background-color: {PALETTE["primary"]};
    color: white;
    border: none;
    border-radius: 6px;
    padding: 4px 14px;
    font-size: 13px;
    font-weight: 500;
}}

QPushButton#downloadButton:hover {{
    background-color: {PALETTE["primary_hover"]};
}}

QPushButton#downloadButton:disabled {{
    background-color: {PALETTE["border"]};
    color: {PALETTE["text_secondary"]};
}}

QLineEdit {{
    background-color: {PALETTE["surface"]};
    border: 1px solid {PALETTE["border"]};
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 14px;
    selection-background-color: {PALETTE["primary"]};
}}

QLineEdit:focus {{
    border: 1px solid {PALETTE["primary"]};
}}

QPushButton {{
    background-color: {PALETTE["primary"]};
    color: white;
    border: none;
    border-radius: 8px;
    padding: 8px 16px;
    font-size: 14px;
    font-weight: 500;
}}

QPushButton:hover {{
    background-color: {PALETTE["primary_hover"]};
}}

QPushButton:pressed {{
    background-color: {PALETTE["primary"]};
}}

QPushButton#refreshButton {{
    background-color: {PALETTE["surface"]};
    color: {PALETTE["text"]};
    border: 1px solid {PALETTE["border"]};
}}

QPushButton#refreshButton:hover {{
    background-color: {PALETTE["surface_hover"]};
}}

QPushButton#settingsButton {{
    background-color: {PALETTE["surface"]};
    color: {PALETTE["text"]};
    border: 1px solid {PALETTE["border"]};
}}

QPushButton#settingsButton:hover {{
    background-color: {PALETTE["surface_hover"]};
}}

QPushButton#downloadManagerButton {{
    background-color: {PALETTE["primary"]};
    color: white;
    border: none;
}}

QPushButton#downloadManagerButton:hover {{
    background-color: {PALETTE["primary_hover"]};
}}

QScrollArea {{
    border: none;
    background-color: transparent;
}}

QScrollBar:vertical {{
    background-color: transparent;
    width: 8px;
    border-radius: 4px;
}}

QScrollBar::handle:vertical {{
    background-color: {PALETTE["border"]};
    border-radius: 4px;
    min-height: 32px;
}}

QScrollBar::handle:vertical:hover {{
    background-color: #D1D5DB;
}}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QLabel#titleLabel {{
    font-size: 24px;
    font-weight: 700;
    color: {PALETTE["text"]};
}}

QLabel#statusLabel {{
    font-size: 13px;
    color: {PALETTE["text_secondary"]};
}}
"""
