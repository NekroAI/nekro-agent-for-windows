STYLESHEET = """
QMainWindow {
    background: #f4f7fb;
}

QWidget {
    color: #183247;
    font-family: "Microsoft YaHei UI";
    font-size: 13px;
}

QDialog,
QMessageBox {
    background: #f8fbff;
}

QDialog QLabel,
QMessageBox QLabel {
    color: #284257;
}

QDialog QPushButton,
QMessageBox QPushButton {
    min-height: 36px;
    background: #ffffff;
    border: 1px solid #d7e2ec;
    border-radius: 8px;
    padding: 0 16px;
    color: #264057;
    font-size: 13px;
    font-weight: 600;
}

QDialog QPushButton:hover,
QMessageBox QPushButton:hover {
    border-color: #8fc5dd;
    background: #f6fbfe;
}

QProgressBar {
    border: none;
    background: #e7eef5;
    border-radius: 4px;
    min-height: 8px;
}

QProgressBar::chunk {
    background: #3db6d1;
    border-radius: 4px;
}

QFrame#Sidebar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #fff9f8, stop:1 #fff3f1);
    border-right: 1px solid #ecd9d6;
}

QPushButton#SidebarToggle {
    background: rgba(255, 255, 255, 0.7);
    border: 1px solid #ecd9d6;
    border-radius: 8px;
    color: #bf655d;
    font-size: 14px;
    font-weight: 700;
}

QPushButton#SidebarToggle:hover {
    background: #ffffff;
    border-color: #e7beb6;
}

QLabel#SidebarEyebrow {
    color: #d9807a;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
}

QLabel#SidebarTitle {
    color: #24384a;
    font-size: 20px;
    font-weight: 700;
}

QLabel#SidebarSubtitle {
    color: #7d8fa0;
    font-size: 12px;
}

QPushButton[nav="true"] {
    background: transparent;
    border: 1px solid #f1e4e0;
    border-radius: 8px;
    padding: 12px 14px;
    text-align: left;
    color: #5d7489;
    font-size: 14px;
    font-weight: 600;
}

QPushButton[nav="true"]:hover {
    background: rgba(255, 255, 255, 0.75);
    border-color: #e8d5d1;
    color: #22394c;
}

QPushButton[nav="true"]:checked {
    background: #ffe8e3;
    border-color: #f1c4bc;
    color: #bf655d;
}

QPushButton[nav="true"][collapsed="true"] {
    padding: 12px 0;
    text-align: center;
    font-size: 13px;
}

QPushButton#SidebarFootBtn {
    background: rgba(255, 255, 255, 0.68);
    border: 1px solid #eddeda;
    border-radius: 10px;
    padding: 6px 12px;
    color: #586d7e;
    font-size: 14px;
    font-weight: 700;
    text-align: center;
}

QPushButton#SidebarFootBtn:hover {
    background: #ffffff;
    border-color: #e7beb6;
    color: #bf655d;
}

QLabel#SidebarFootnote {
    color: #8a98a6;
    font-size: 12px;
}

QFrame#HeroCard {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #fff7f6, stop:1 #fefcfc);
    border: 1px solid #efd7d2;
    border-radius: 8px;
}

QLabel#HeroEyebrow {
    color: #d9817c;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
}

QLabel#HeroTitle {
    color: #24384a;
    font-size: 24px;
    font-weight: 700;
}

QLabel#HeroDesc {
    color: #6e8396;
    font-size: 13px;
    line-height: 1.4em;
}

QLabel#StatusBadge {
    background: #fff0ed;
    border: 1px solid #f1d0c9;
    border-radius: 8px;
    color: #ba665f;
    font-size: 12px;
    font-weight: 700;
    padding: 6px 12px;
}

QLabel#FeatureBadge {
    background: #edf8ff;
    border: 1px solid #c7e3f7;
    border-radius: 8px;
    color: #1b6db4;
    font-size: 12px;
    font-weight: 700;
    padding: 6px 12px;
}

QLabel#QuickLabel {
    color: #cf7a74;
    font-size: 11px;
    font-weight: 600;
}

QLabel#QuickValue {
    color: #274055;
    font-size: 14px;
    font-weight: 700;
}

QPushButton#HeroPrimary,
QPushButton#HeroSecondary {
    min-height: 40px;
    border-radius: 8px;
    padding: 0 18px;
    font-size: 13px;
    font-weight: 700;
}

QPushButton#HeroPrimary {
    background: #e88478;
    color: #ffffff;
    border: 1px solid #e88478;
}

QPushButton#HeroPrimary:hover {
    background: #ef9488;
    border-color: #ef9488;
}

QPushButton#HeroSecondary {
    background: #ffffff;
    color: #2a4358;
    border: 1px solid #d7e2ec;
}

QPushButton#HeroSecondary:hover {
    border-color: #8fc5dd;
    background: #f5fbfe;
}

QFrame#MetricCard,
QFrame#SectionCard {
    background: #ffffff;
    border: 1px solid #dfe7ef;
    border-radius: 8px;
}

QTabWidget#BrowserTabs::pane {
    border: 1px solid #dfe7ef;
    border-radius: 8px;
    background: #ffffff;
    top: 1px;
}

QTabWidget#BrowserTabs QTabBar {
    left: 8px;
}

QTabWidget#BrowserTabs QTabBar::tab {
    min-height: 32px;
    padding: 0 14px;
    margin-right: 4px;
    margin-top: 6px;
    background: #eef3f8;
    border: 1px solid #d7e2ec;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    color: #607789;
    font-weight: 600;
}

QTabWidget#BrowserTabs QTabBar::tab:hover {
    background: #f6f9fc;
    border-color: #cddae6;
    color: #3f5669;
}

QTabWidget#BrowserTabs QTabBar::tab:selected {
    background: #ffffff;
    border-color: #dfe7ef;
    border-bottom-color: #ffffff;
    color: #274055;
}

QLineEdit#BrowserAddressBar {
    background: #f7f9fc;
    border: 1px solid #d7e2ec;
    border-radius: 17px;
    padding: 6px 14px;
    min-height: 22px;
    color: #486173;
    selection-background-color: #c8e6f5;
}

QLineEdit#BrowserAddressBar:focus {
    border-color: #8fc5dd;
    background: #ffffff;
}

QFrame#MetricCard[accent="blue"] {
    border-top: 2px solid #57bfd6;
}

QFrame#MetricCard[accent="green"] {
    border-top: 2px solid #54c08a;
}

QFrame#MetricCard[accent="amber"] {
    border-top: 2px solid #e4b263;
}

QFrame#MetricCard[accent="red"] {
    border-top: 2px solid #e88478;
}

QLabel#MetricLabel {
    color: #8395a4;
    font-size: 11px;
    font-weight: 700;
}

QLabel#MetricValue {
    color: #264057;
    font-size: 18px;
    font-weight: 700;
}

QLabel#MetricHint,
QLabel#SectionDesc {
    color: #8092a2;
    font-size: 12px;
}

QLabel#SectionTitle {
    color: #264057;
    font-size: 18px;
    font-weight: 700;
}

QPushButton[variant="default"],
QPushButton[variant="primary"],
QPushButton[variant="danger"] {
    background: #ffffff;
    border: 1px solid #dfe7ef;
    border-radius: 8px;
    text-align: left;
}

QPushButton[variant="default"]:hover {
    border-color: #8fc5dd;
    background: #f6fbfe;
}

QPushButton[variant="primary"]:hover {
    border-color: #e8a69a;
    background: #fff8f7;
}

QPushButton[variant="danger"]:hover {
    border-color: #e8b4b4;
    background: #fff9f9;
}

QLabel#ActionBadge {
    background: #eef9fc;
    border: 1px solid #d7f0f6;
    border-radius: 8px;
    color: #3db6d1;
    font-size: 13px;
    font-weight: 700;
}

QPushButton[variant="primary"] QLabel#ActionBadge {
    background: #fff1ee;
    border-color: #f7d8d1;
    color: #db7f72;
}

QPushButton[variant="danger"] QLabel#ActionBadge {
    background: #fff2f2;
    border-color: #f3d7d7;
    color: #d26b6b;
}

QLabel#ActionTitle {
    font-size: 15px;
    font-weight: 700;
    color: #264057;
}

QLabel#ActionDesc {
    font-size: 12px;
    color: #8092a2;
    line-height: 1.35em;
}

QLabel[role="dialog_title"] {
    color: #264057;
    font-size: 18px;
    font-weight: 700;
}

QLabel[role="dialog_desc"] {
    color: #7a8d9f;
    font-size: 13px;
}

QLabel[role="info_block"] {
    background: #fbfdff;
    border: 1px solid #dfe7ef;
    border-radius: 8px;
    padding: 14px;
    font-size: 13px;
}

QPushButton#SegmentBtn {
    min-height: 34px;
    background: transparent;
    border: 1px solid #e2e8ef;
    border-radius: 8px;
    color: #7d8fa0;
    font-size: 13px;
    font-weight: 600;
    padding: 0 14px;
}

QPushButton#SegmentBtn:hover {
    background: rgba(255, 255, 255, 0.6);
    border-color: #f1d0c9;
    color: #3d5366;
}

QPushButton#SegmentBtn:checked {
    background: #ffe8e3;
    border-color: #f1c4bc;
    color: #bf655d;
}

QTextEdit#LogViewer {
    background: #0f2032;
    color: #dfeaf6;
    border: 1px solid #20384f;
    border-radius: 8px;
    padding: 16px;
    font-family: "Cascadia Mono", Consolas, "Courier New", "Microsoft YaHei UI";
    font-size: 13px;
}

QLineEdit,
QComboBox {
    background: #ffffff;
    border: 1px solid #d7e2ec;
    border-radius: 8px;
    padding: 8px 12px;
    min-height: 22px;
    color: #264057;
    font-size: 13px;
    selection-background-color: #c8e6f5;
    selection-color: #183247;
}

QLineEdit:focus,
QComboBox:hover {
    border-color: #8fc5dd;
}

QComboBox:on {
    background: #ffffff;
}

QComboBox {
    padding-right: 34px;
}

QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 30px;
    border: none;
    border-left: 1px solid #e4ebf2;
    background: #ffffff;
    border-top-right-radius: 8px;
    border-bottom-right-radius: 8px;
}

QComboBox:hover::drop-down {
    border-left-color: #d4e6ef;
    background: #ffffff;
}

QComboBox::down-arrow {
    image: url(assets/chevron-down.svg);
    width: 10px;
    height: 6px;
}

QComboBox::down-arrow:on {
    top: 1px;
}

QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #d7e2ec;
    selection-background-color: #fff0ed;
    selection-color: #c46b62;
    outline: none;
}

QCheckBox {
    color: #274055;
    font-size: 14px;
    spacing: 10px;
}

QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border: 1px solid #bfd0dd;
    border-radius: 5px;
    background: #ffffff;
}

QCheckBox::indicator:checked {
    background: #57bfd6;
    border-color: #57bfd6;
}

QPushButton {
    outline: none;
}

QPushButton:focus {
    outline: none;
}

QPushButton[role="danger"] {
    min-height: 36px;
    background: #c94f63;
    color: white;
    border: 1px solid #c94f63;
    border-radius: 10px;
    padding: 0 16px;
    font-size: 13px;
    font-weight: 600;
}

QPushButton[role="danger"]:hover {
    background: #b84558;
    border-color: #b84558;
}

QPushButton[role="primary"] {
    min-height: 36px;
    background: #1b6db4;
    color: white;
    border: 1px solid #1b6db4;
    border-radius: 10px;
    padding: 0 16px;
    font-size: 13px;
    font-weight: 600;
}

QPushButton[role="primary"]:hover {
    background: #185f9d;
    border-color: #185f9d;
}
"""
