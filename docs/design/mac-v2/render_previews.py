"""Render three static Mac product concepts. These are design previews, not app screens."""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent
W, H = 1600, 1060
FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"


def f(size, bold=False):
    return ImageFont.truetype(FONT, size, index=2 if bold else 0)


def rr(d, box, fill, r=12, outline=None, width=1):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def t(d, xy, value, size=18, color="#222831", bold=False, anchor=None):
    d.text(xy, value, font=f(size, bold), fill=color, anchor=anchor)


def line(d, xy, color="#E5E8E9", width=1):
    d.line(xy, fill=color, width=width)


def dot(d, x, y, fill, r=5):
    d.ellipse((x-r, y-r, x+r, y+r), fill=fill)


def pill(d, box, label, bg, fg, size=14):
    rr(d, box, bg, r=(box[3]-box[1])//2)
    t(d, ((box[0]+box[2])/2, (box[1]+box[3])/2), label, size, fg, True, "mm")


def board(name, desc, palette, accent):
    im = Image.new("RGB", (W, H), palette["board"])
    d = ImageDraw.Draw(im)
    t(d, (64, 37), name, 34, palette["title"], True)
    t(d, (66, 83), desc, 17, palette["muted"])
    rr(d, (64, 137, 1536, 835), palette["window"], 19, palette["border"], 2)
    rr(d, (64, 137, 1536, 172), palette["chrome"], 19)
    d.rectangle((64, 157, 1536, 172), fill=palette["chrome"])
    for x, c in [(87, "#FF615A"), (108, "#FFBD2E"), (129, "#28C840")]:
        dot(d, x, 154, c, 6)
    t(d, (800, 154), "知间  /  个人知识库", 13, palette["chromeText"], anchor="mm")
    return im, d


def login_strip(d, palette, accent, mode, note):
    t(d, (65, 861), "登录体验", 16, palette["title"], True)
    rr(d, (64, 896, 626, 1026), palette["login"], 14, palette["border"])
    rr(d, (83, 915, 122, 954), accent, 10)
    t(d, (102, 935), "知", 19, "#FFFFFF", True, "mm")
    t(d, (138, 915), "欢迎回到知间", 19, palette["title"], True)
    t(d, (138, 946), mode, 14, palette["muted"])
    rr(d, (83, 980, 368, 1009), palette["field"], 7, palette["border"])
    t(d, (95, 985), "邮箱地址  ·  chen@example.com", 13, palette["muted"])
    pill(d, (384, 978, 603, 1009), "继续登录  →", accent, "#FFFFFF", 13)
    t(d, (663, 885), "这个方向的重点", 16, palette["title"], True)
    t(d, (663, 927), note[0], 17, palette["title"])
    t(d, (663, 959), note[1], 17, palette["muted"])
    t(d, (663, 991), "概念预览 · UI 与登录流程尚未实现", 13, palette["muted"])


def concept_a():
    p = dict(board="#EDF2F2", title="#1B3036", muted="#64777C", window="#FFFFFF",
             border="#D5E1E1", chrome="#F5F8F8", chromeText="#728186", login="#FFFFFF", field="#F5F8F8")
    ac = "#246D79"
    im, d = board("方案 A · 档案工作台", "层级清楚、信息密度适中：从知识树选择范围，再在列表和阅读器之间连续工作。", p, ac)
    # Three-pane workspace.
    d.rectangle((65, 172, 317, 834), fill="#F4F8F7")
    line(d, (317, 172, 317, 834), "#DFE8E7")
    d.rectangle((318, 172, 674, 834), fill="#FCFDFD")
    line(d, (674, 172, 674, 834), "#E1E8E8")
    rr(d, (84, 194, 118, 228), ac, 9)
    t(d, (101, 211), "知", 17, "#FFFFFF", True, "mm")
    t(d, (130, 200), "知间", 18, p["title"], True)
    t(d, (129, 224), "陈璟的空间  v", 12, p["muted"])
    rr(d, (84, 258, 297, 297), "#FFFFFF", 8, "#D9E5E3")
    t(d, (101, 268), "   搜索全部知识     Cmd K", 13, p["muted"])
    pill(d, (84, 314, 297, 353), "+   快速采集", ac, "#FFFFFF", 15)
    t(d, (87, 381), "知识树", 12, p["muted"], True)
    t(d, (277, 380), "+", 17, ac, True)
    for y, s in [(417, "-   工作"), (454, "    -   产品研发"), (491, "         商品管理页"),
                 (528, "         Agent 产品"), (571, "    +   阅读与研究"),
                 (617, "+   生活"), (663, "+   稍后整理")]:
        if y == 491:
            rr(d, (78, 483, 304, 516), "#DCECEB", 7)
        t(d, (91, y), s, 15, ac if y == 491 else p["title"], y in (417, 491))
    line(d, (84, 752, 297, 752), "#DCE6E4")
    t(d, (88, 774), "●   Agent 访问权限", 14, p["title"])
    t(d, (88, 806), "●  已登录  chen@example.com", 12, p["muted"])
    # Source list.
    t(d, (343, 205), "商品管理页", 24, p["title"], True)
    t(d, (344, 245), "32 条资料   ·   最近更新", 13, p["muted"])
    rr(d, (339, 278, 653, 314), "#F3F7F7", 8)
    t(d, (353, 287), "  在当前知识域中搜索", 13, p["muted"])
    t(d, (344, 339), "全部  32", 14, ac, True)
    t(d, (430, 339), "PDF  8", 14, p["muted"])
    t(d, (514, 339), "笔记  12", 14, p["muted"])
    line(d, (342, 368, 653, 368), "#E7EEEE")
    rows = [
        (382, "PDF", "虚拟列表在大数据量场景中的取舍", "研发资料  ·  9 月 24 日", "3 万条商品数据的滚动性能测试…"),
        (501, "笔记", "商品列表性能排查记录", "个人经验  ·  9 月 21 日", "采用分页 + 虚拟滚动后，首屏…"),
        (620, "网页", "React Window 实践要点", "外部资料  ·  9 月 18 日", "动态高度与滚动位置的处理…"),
    ]
    for y, typ, title, meta, snippet in rows:
        if y == 382: rr(d, (330, y-7, 661, y+101), "#E8F2F1", 10)
        pill(d, (343, y+4, 388, y+28), typ, "#E4EEEC", ac, 11)
        t(d, (343, y+36), title, 15, p["title"], True)
        t(d, (343, y+62), meta, 12, p["muted"])
        t(d, (343, y+83), snippet, 12, p["muted"])
    # Reader.
    t(d, (708, 195), "工作  /  产品研发  /  商品管理页", 13, p["muted"])
    t(d, (1455, 195), "↗  ···", 19, p["muted"])
    line(d, (698, 229, 1513, 229), "#E8EEEE")
    pill(d, (710, 260, 760, 288), "PDF", "#E7EFEE", ac, 12)
    pill(d, (773, 260, 851, 288), "已识别", "#E2F2E7", "#29714A", 12)
    t(d, (710, 309), "虚拟列表在大数据量", 30, p["title"], True)
    t(d, (710, 354), "场景中的取舍", 30, p["title"], True)
    t(d, (711, 409), "来源文件  ·  18 页  ·  版本 2  ·  更新于 9 月 24 日", 14, p["muted"])
    line(d, (710, 442, 1495, 442), "#E8EEEE")
    t(d, (711, 461), "原文", 15, ac, True)
    t(d, (781, 461), "证据  12", 15, p["muted"])
    t(d, (890, 461), "个人上下文", 15, p["muted"])
    t(d, (713, 515), "当列表数据量达到数万条时，页面不应一次渲染全部行。", 18, p["title"])
    t(d, (713, 552), "虚拟列表只保留视口附近的节点，可以明显降低首次渲染", 18, p["title"])
    t(d, (713, 589), "和滚动时的布局开销，但需要处理动态高度与定位问题。", 18, p["title"])
    rr(d, (710, 648, 1489, 771), "#F1F7F6", 11)
    t(d, (730, 666), "我的备注", 13, ac, True)
    t(d, (730, 694), "给商品管理页参考：目前约 3 万条记录，先验证分页与虚拟滚动组合。", 16, p["title"])
    t(d, (730, 736), "关联项目  商品管理页     ·     验证状态  尚未实践", 13, p["muted"])
    login_strip(d, p, ac, "邮箱验证码 / 会话保持 / 账户切换", (
        "知识域树 + 资料列表 + 阅读器同时可见。",
        "最适合先做生产版基础框架，扩展成本可控。"))
    im.save(OUT / "方案A-档案工作台.png")


def concept_b():
    p = dict(board="#EAE9E5", title="#202831", muted="#6E767A", window="#151A20",
             border="#343A3F", chrome="#20262C", chromeText="#A5AAB0", login="#FFFFFF", field="#F4F4F2")
    ac = "#AC7044"
    im, d = board("方案 B · 沉浸阅读", "把阅读、批注和证据定位放在中心；知识树与资料队列常驻，但更安静。", p, ac)
    ink, muted, edge = "#E6E2DB", "#9DA6AD", "#303941"
    d.rectangle((65, 172, 132, 834), fill="#11161B")
    d.rectangle((133, 172, 364, 834), fill="#1A2026")
    line(d, (364, 172, 364, 834), edge)
    d.rectangle((365, 172, 1183, 834), fill="#171C22")
    line(d, (1183, 172, 1183, 834), edge)
    d.rectangle((1184, 172, 1535, 834), fill="#1D242B")
    # Rail and tree.
    rr(d, (80, 194, 118, 232), ac, 10)
    t(d, (99, 213), "知", 17, "#FFFFFF", True, "mm")
    for y, symbol, active in [(282, "首", False), (347, "阅", True), (412, "搜", False), (477, "+", False)]:
        if active: rr(d, (75, y-15, 123, y+33), "#303A40", 12)
        t(d, (98, y+6), symbol, 26, "#D5B48B" if active else muted, anchor="mm")
    dot(d, 99, 802, "#6FA882", 16)
    t(d, (99, 801), "陈", 13, "#FFFFFF", True, "mm")
    t(d, (160, 202), "阅读空间", 19, ink, True)
    t(d, (160, 233), "已登录 · 陈璟", 12, muted)
    rr(d, (151, 269, 345, 305), "#242C33", 8)
    t(d, (165, 278), "  搜索我的知识", 13, muted)
    t(d, (155, 333), "阅读队列", 12, muted, True)
    for y, label, n in [(368, "收集箱", "08"), (404, "正在阅读", "12"), (440, "已读", "216")]:
        if y == 404: rr(d, (146, y-6, 351, y+27), "#333941", 7)
        t(d, (158, y), label, 15, ink if y == 404 else muted, y == 404)
        t(d, (329, y), n, 12, muted, anchor="ra")
    line(d, (153, 488, 344, 488), edge)
    t(d, (155, 512), "知识树", 12, muted, True)
    for y, text_ in [(549, "-  工作"), (586, "    -  产品研发"), (623, "         商品管理页"),
                     (660, "         Agent 产品"), (703, "+  生活")]:
        t(d, (155, y), text_, 14, "#D9B994" if y == 623 else ink)
    # Reader.
    t(d, (403, 199), "←  正在阅读   /   商品管理页", 13, muted)
    t(d, (1102, 199), "A-  A+   ···", 15, muted)
    line(d, (393, 231, 1155, 231), edge)
    t(d, (461, 268), "PDF  ·  第 12 / 18 页", 13, "#D8AD7A", True)
    t(d, (461, 308), "虚拟列表在大数据量", 31, ink, True)
    t(d, (461, 353), "场景中的取舍", 31, ink, True)
    t(d, (461, 408), "来源文件  ·  已识别  ·  预计阅读 8 分钟", 13, muted)
    line(d, (461, 443, 1089, 443), edge)
    t(d, (461, 473), "当列表数据量达到数万条时，页面不应一次渲染", 18, ink)
    t(d, (461, 512), "全部行。虚拟列表只保留视口附近的节点，", 18, ink)
    rr(d, (451, 554, 1103, 636), "#3A342D", 6)
    d.rectangle((451, 554, 455, 636), fill="#DDB278")
    t(d, (472, 564), "可以明显降低首次渲染和滚动时的布局开销，", 18, "#F5E9D8")
    t(d, (472, 602), "但需要处理动态高度与定位问题。", 18, "#F5E9D8")
    t(d, (461, 670), "一个常见的问题是滚动位置跳动。测量每一行的", 18, ink)
    t(d, (461, 708), "实际高度后，再对预估值进行校正。", 18, ink)
    rr(d, (451, 772, 1118, 778), "#323B42", 3)
    rr(d, (451, 772, 814, 778), "#B88052", 3)
    # Context / evidence panel.
    t(d, (1211, 201), "阅读标记", 17, ink, True)
    t(d, (1475, 202), "12 条", 12, muted)
    line(d, (1205, 237, 1513, 237), edge)
    pill(d, (1207, 260, 1267, 287), "证据", "#49382B", "#E8B680", 12)
    t(d, (1208, 310), "第 12 页 · 版本 2", 13, muted)
    rr(d, (1207, 343, 1505, 475), "#2A333A", 9)
    t(d, (1222, 357), "“可以明显降低首次渲染", 16, ink)
    t(d, (1222, 386), " 和滚动时的布局开销…”", 16, ink)
    t(d, (1222, 430), "打开对应原文  ↗", 13, "#D4A46F", True)
    t(d, (1208, 521), "我的理解", 13, muted, True)
    t(d, (1208, 556), "先测量商品管理页真实滚动", 15, ink)
    t(d, (1208, 587), "瓶颈，再决定是否引入虚拟", 15, ink)
    t(d, (1208, 618), "列表。", 15, ink)
    line(d, (1207, 667, 1505, 667), edge)
    t(d, (1208, 690), "版本记录", 13, muted, True)
    t(d, (1208, 728), "●  当前版本  2", 14, ink)
    t(d, (1208, 767), "○  历史版本  1", 14, muted)
    login_strip(d, p, ac, "邮箱验证码 / 设备记忆 / 安全退出", (
        "长文和 PDF 阅读体验最强，证据始终在手边。",
        "适合以“读懂并引用资料”为核心的产品。"))
    im.save(OUT / "方案B-沉浸阅读.png")


def concept_c():
    p = dict(board="#F0EFF7", title="#262840", muted="#75788D", window="#FBFAFD",
             border="#DCDCEB", chrome="#F5F4F9", chromeText="#7F8294", login="#FFFFFF", field="#F5F4F9")
    ac = "#6554A5"
    im, d = board("方案 C · 项目知识中心", "让资料围绕项目、个人判断和 Agent 权限组织；阅读器以侧面板保持上下文。", p, ac)
    d.rectangle((65, 172, 323, 834), fill="#F7F6FB")
    line(d, (323, 172, 323, 834), "#E4E2EE")
    d.rectangle((324, 172, 1057, 834), fill="#FCFBFE")
    line(d, (1057, 172, 1057, 834), "#E5E3EF")
    d.rectangle((1058, 172, 1535, 834), fill="#FFFFFF")
    # Tree/nav.
    rr(d, (84, 194, 121, 231), ac, 9)
    t(d, (102, 213), "知", 17, "#FFFFFF", True, "mm")
    t(d, (136, 199), "知间", 19, p["title"], True)
    t(d, (136, 224), "陈璟的知识空间", 12, p["muted"])
    for y, label, selected in [(274, "   总览", False), (315, "   收集箱", False),
                               (356, "   全局搜索", False), (397, "   Agent 连接", False)]:
        t(d, (92, y), label, 15, p["title"] if not selected else ac)
    line(d, (87, 436, 302, 436), "#E5E3EC")
    t(d, (89, 460), "知识树 / 项目", 12, p["muted"], True)
    t(d, (285, 458), "+", 18, ac, True)
    for y, label in [(499, "-  工作"), (536, "    -  产品研发"), (573, "         商品管理页"),
                     (610, "         Agent 产品"), (653, "+  生活")]:
        if y == 573: rr(d, (80, 566, 309, 602), "#EAE6F5", 8)
        t(d, (91, y), label, 14, ac if y == 573 else p["title"], y == 573)
    line(d, (86, 750, 303, 750), "#E5E3EC")
    dot(d, 103, 792, ac, 17)
    t(d, (103, 791), "陈", 12, "#FFFFFF", True, "mm")
    t(d, (129, 776), "陈璟", 13, p["title"], True)
    t(d, (129, 800), "个人空间 · 已登录", 12, p["muted"])
    # Main context dashboard.
    t(d, (355, 200), "工作  /  产品研发  /  商品管理页", 13, p["muted"])
    t(d, (355, 239), "商品管理页", 28, p["title"], True)
    t(d, (356, 284), "围绕列表性能、体验和实施记录沉淀的项目知识。", 15, p["muted"])
    rr(d, (355, 328, 1026, 459), "#F0EDF8", 13)
    t(d, (375, 347), "项目上下文", 13, ac, True)
    t(d, (375, 378), "当前问题：3 万条商品数据，筛选和滚动出现明显卡顿。", 17, p["title"], True)
    t(d, (375, 414), "下一步：验证分页 + 虚拟滚动，记录首屏与滚动帧率。", 15, p["muted"])
    t(d, (355, 489), "关键资料", 19, p["title"], True)
    t(d, (946, 493), "查看全部  →", 13, ac, True)
    cards = [(355, "PDF", "虚拟列表在大数据量", "场景中的取舍", "证据 12 · 版本 2"),
             (691, "笔记", "商品列表性能", "排查记录", "个人经验 · 已验证")]
    for x, typ, a, b, meta in cards:
        rr(d, (x, 526, x+318, 681), "#FFFFFF", 11, "#E0DDEA")
        pill(d, (x+17, 543, x+74, 570), typ, "#EEEAF7", ac, 11)
        t(d, (x+17, 583), a, 16, p["title"], True)
        t(d, (x+17, 611), b, 16, p["title"], True)
        t(d, (x+17, 648), meta, 12, p["muted"])
    rr(d, (355, 715, 1026, 807), "#FFFFFF", 11, "#E0DDEA")
    t(d, (375, 732), "Agent 可访问范围", 13, ac, True)
    t(d, (375, 761), "工作知识域已授权  ·  最近调用 9 月 25 日 14:30", 15, p["title"])
    t(d, (942, 761), "管理  →", 13, ac, True)
    # Reader side panel.
    t(d, (1084, 199), "资料阅读器", 14, p["muted"], True)
    t(d, (1474, 198), "+   ×", 17, p["muted"])
    line(d, (1081, 229, 1511, 229), "#E8E5EE")
    pill(d, (1083, 258, 1138, 285), "PDF", "#EFEAF8", ac, 12)
    t(d, (1084, 310), "虚拟列表在大数据量", 22, p["title"], True)
    t(d, (1084, 344), "场景中的取舍", 22, p["title"], True)
    t(d, (1084, 385), "第 12 / 18 页  ·  已识别", 13, p["muted"])
    line(d, (1082, 417, 1510, 417), "#E8E5EE")
    t(d, (1083, 438), "原文", 14, ac, True)
    t(d, (1152, 438), "证据", 14, p["muted"])
    t(d, (1222, 438), "版本", 14, p["muted"])
    t(d, (1084, 488), "当列表数据量达到数万条时，", 17, p["title"])
    t(d, (1084, 525), "页面不应一次渲染全部行。", 17, p["title"])
    rr(d, (1082, 571, 1507, 663), "#F3F0FA", 8)
    t(d, (1098, 585), "虚拟列表只保留视口附近的节点，", 16, p["title"])
    t(d, (1098, 619), "可以降低首次渲染的布局开销。", 16, p["title"])
    t(d, (1083, 707), "我的判断", 13, ac, True)
    t(d, (1083, 739), "先做真实场景测试，再决定实现方案。", 15, p["title"])
    pill(d, (1083, 781, 1507, 811), "打开完整阅读器  ↗", ac, "#FFFFFF", 13)
    login_strip(d, p, ac, "邮箱验证码 / 空间选择 / 权限概览", (
        "项目上下文、资料和 Agent 权限形成闭环。",
        "产品差异化最强，但项目视图需要更多后端能力。"))
    im.save(OUT / "方案C-项目知识中心.png")


if __name__ == "__main__":
    concept_a()
    concept_b()
    concept_c()
    print("Rendered 3 previews to", OUT)
