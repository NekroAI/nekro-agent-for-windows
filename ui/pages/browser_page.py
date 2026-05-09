from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout, QLineEdit, QPushButton, QSizePolicy,
    QTabWidget, QVBoxLayout, QWidget,
)

from ui.widgets import SectionCard


class BrowserPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.w = window

        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 30, 34, 30)
        layout.setSpacing(18)

        card = SectionCard("服务访问", "在应用内直接访问管理界面，仍可按需切换到系统浏览器。")
        card_layout = card.body_layout()

        target_row = QHBoxLayout()
        self.w.btn_browser_nekro = QPushButton("Nekro Agent")
        self.w.btn_browser_nekro.setObjectName("SegmentBtn")
        self.w.btn_browser_nekro.setCheckable(True)
        self.w.btn_browser_nekro.clicked.connect(lambda: self.w._set_browser_target("nekro"))
        target_row.addWidget(self.w.btn_browser_nekro)

        self.w.btn_browser_napcat = QPushButton("NapCat")
        self.w.btn_browser_napcat.setObjectName("SegmentBtn")
        self.w.btn_browser_napcat.setCheckable(True)
        self.w.btn_browser_napcat.clicked.connect(lambda: self.w._set_browser_target("napcat"))
        self.w.btn_browser_napcat.setVisible(self.w.config.get("deploy_mode") == "napcat")
        target_row.addWidget(self.w.btn_browser_napcat)
        target_row.addStretch()
        card_layout.addLayout(target_row)

        nav_row = QHBoxLayout()
        self.w.browser_back_btn = QPushButton("后退")
        self.w.browser_back_btn.setObjectName("SegmentBtn")
        self.w.browser_back_btn.clicked.connect(self.w._browser_go_back)
        nav_row.addWidget(self.w.browser_back_btn)

        self.w.browser_forward_btn = QPushButton("前进")
        self.w.browser_forward_btn.setObjectName("SegmentBtn")
        self.w.browser_forward_btn.clicked.connect(self.w._browser_go_forward)
        nav_row.addWidget(self.w.browser_forward_btn)

        self.w.browser_reload_btn = QPushButton("刷新")
        self.w.browser_reload_btn.setObjectName("SegmentBtn")
        self.w.browser_reload_btn.clicked.connect(self.w._reload_browser_view)
        nav_row.addWidget(self.w.browser_reload_btn)

        self.w.browser_url_label = QLineEdit()
        self.w.browser_url_label.setObjectName("BrowserAddressBar")
        self.w.browser_url_label.setReadOnly(True)
        self.w.browser_url_label.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.w.browser_url_label.setMinimumWidth(200)
        self.w.browser_url_label.setPlaceholderText("当前页面地址")
        nav_row.addWidget(self.w.browser_url_label, 1)
        card_layout.addLayout(nav_row)

        action_row = QHBoxLayout()
        self.w.browser_fill_credentials_btn = QPushButton("填充凭据")
        self.w.browser_fill_credentials_btn.setObjectName("SegmentBtn")
        self.w.browser_fill_credentials_btn.clicked.connect(self.w._fill_browser_credentials)
        self.w.browser_fill_credentials_btn.setToolTip("将已保存的登录凭据填入当前页面")
        action_row.addWidget(self.w.browser_fill_credentials_btn)

        self.w.browser_config_napcat_btn = QPushButton("一键配网")
        self.w.browser_config_napcat_btn.setObjectName("SegmentBtn")
        self.w.browser_config_napcat_btn.clicked.connect(self.w._configure_napcat_network)
        self.w.browser_config_napcat_btn.setToolTip("直接写入 NapCat 配置文件并重启 NapCat 服务")
        self.w.browser_config_napcat_btn.setVisible(False)
        action_row.addWidget(self.w.browser_config_napcat_btn)

        self.w.browser_open_external_btn = QPushButton("在系统浏览器打开")
        self.w.browser_open_external_btn.setObjectName("SegmentBtn")
        self.w.browser_open_external_btn.clicked.connect(self.w._open_current_in_browser)
        action_row.addWidget(self.w.browser_open_external_btn)

        self.w.browser_devtools_btn = QPushButton("开发者工具")
        self.w.browser_devtools_btn.setObjectName("SegmentBtn")
        self.w.browser_devtools_btn.clicked.connect(self.w._open_browser_devtools)
        self.w.browser_devtools_btn.setVisible(self.w._advanced_features_enabled())
        action_row.addWidget(self.w.browser_devtools_btn)
        action_row.addStretch()
        card_layout.addLayout(action_row)

        from ui.main_window import BrowserTabBar

        self.w.browser_tabs = QTabWidget()
        self.w.browser_tabs.setObjectName("BrowserTabs")
        self.w.browser_tabs.setTabBar(BrowserTabBar(self.w.browser_tabs))
        self.w.browser_tabs.setDocumentMode(False)
        self.w.browser_tabs.setMovable(True)
        self.w.browser_tabs.setTabsClosable(False)
        self.w.browser_tabs.setUsesScrollButtons(True)
        self.w.browser_tabs.tabBar().tabCloseRequested.connect(self.w._close_browser_tab)
        self.w.browser_tabs.currentChanged.connect(self.w._on_browser_tab_changed)
        card_layout.addWidget(self.w.browser_tabs, 1)

        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(card, 1)
        self.w._create_browser_tab(switch_to=True, title=self.w._target_label("nekro"))
        self.w._set_browser_target("nekro")
        self.w._refresh_browser_nav_buttons()
