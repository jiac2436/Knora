from PIL import Image, ImageDraw, ImageFont, ImageFilter
from pathlib import Path

OUT = Path(__file__).parent
W, H = 1600, 1000
FONT = '/System/Library/Fonts/Hiragino Sans GB.ttc'
FONT_LATIN = '/System/Library/Fonts/HelveticaNeue.ttc'
BG = '#11151a'
SIDE = '#1c2229'
PANEL = '#171c22'
SURFACE = '#252c34'
EDGE = '#343c45'
TEXT = '#eef0f1'
MUTED = '#98a1aa'
FAINT = '#6e7882'
ACCENT = '#c08a5d'
GREEN = '#50c783'

def f(sz, latin=False):
    return ImageFont.truetype(FONT_LATIN if latin else FONT, sz)

def txt(d, xy, s, size=18, color=TEXT, weight=False, anchor=None):
    d.text(xy, s, font=f(size), fill=color, anchor=anchor, stroke_width=0)

def line(d, xy, color=EDGE, width=1): d.line(xy, fill=color, width=width)
def rr(d, box, fill, radius=14, outline=None, width=1): d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)

def icon(d, name, x, y, c=MUTED, z=20):
    if name == 'search':
        d.ellipse((x,y,x+z*.68,y+z*.68), outline=c, width=2)
        line(d,(x+z*.55,y+z*.55,x+z,y+z),c,2)
    elif name == 'folder':
        d.rounded_rectangle((x,y+5,x+z,y+z*.8),radius=3,outline=c,width=2)
        line(d,(x+2,y+5,x+z*.38,y+5),c,2)
        line(d,(x+2,y+2,x+z*.36,y+2),c,2)
    elif name == 'doc':
        d.rounded_rectangle((x+3,y,x+z*.85,y+z),radius=2,outline=c,width=2)
        line(d,(x+7,y+z*.52,x+z*.68,y+z*.52),c,2)
        line(d,(x+7,y+z*.7,x+z*.58,y+z*.7),c,2)
    elif name == 'tag':
        d.polygon([(x,y+4),(x+z*.6,y+4),(x+z,y+z*.5),(x+z*.58,y+z),(x,y+z*.48)],outline=c)
        d.ellipse((x+4,y+7,x+7,y+10),fill=c)
    elif name == 'calendar':
        d.rounded_rectangle((x,y+3,x+z,y+z),radius=3,outline=c,width=2)
        line(d,(x,y+z*.34,x+z,y+z*.34),c,2)
        line(d,(x+z*.25,y,x+z*.25,y+6),c,2); line(d,(x+z*.75,y,x+z*.75,y+6),c,2)
    elif name == 'link':
        d.arc((x,y+4,x+z*.7,y+z*.78),35,280,fill=c,width=2)
        d.arc((x+z*.3,y,x+z,y+z*.75),215,460,fill=c,width=2)
    elif name == 'chevron':
        line(d,(x,y+3,x+5,y+8,x,y+13),c,2)
    elif name == 'panel':
        d.rounded_rectangle((x,y,x+z,y+z*.85),radius=3,outline=c,width=2)
        line(d,(x+z*.58,y,x+z*.58,y+z*.85),c,2)
    elif name == 'lines':
        for i in range(3): line(d,(x,y+i*6,x+z,y+i*6),c,2)

def chrome(d, title, subtitle):
    d.rectangle((0,0,W,H), fill='#101318')
    txt(d,(60,31),title,29,TEXT)
    txt(d,(61,71),subtitle,16,MUTED)
    rr(d,(43,111,1557,960),'#15191e',18,EDGE)
    rr(d,(43,111,1557,151),'#2b2a2d',18)
    d.rectangle((44,133,1556,151),fill='#2b2a2d')
    for i,c in enumerate(('#fa6058','#f6bf4f','#48c86a')): d.ellipse((60+i*26,124,76+i*26,140),fill=c)
    txt(d,(738,119),'知识收集箱',17,'#d8d9dc')

def rail(d):
    d.rectangle((44,151,119,959),fill='#14191e')
    rr(d,(59,174,104,219),ACCENT,12)
    txt(d,(70,180),'知',26,'#ffffff')
    rr(d,(57,247,106,296),SURFACE,11)
    icon(d,'folder',69,260,ACCENT,25)
    icon(d,'doc',72,325,MUTED,22)
    icon(d,'search',72,895,MUTED,22)

def toolbar(d, x=458, right=1540, evidence=False, search=True):
    d.rectangle((x,151,right,216),fill=PANEL)
    line(d,(x,216,right,216),EDGE)
    txt(d,(x+31,170),'资料  /  阅读器',16,MUTED)
    if search: icon(d,'search',right-160,172,MUTED,20)
    txt(d,(right-117,169),'A-',19,MUTED)
    txt(d,(right-78,169),'A+',19,MUTED)
    rr(d,(right-48,164,right-13,201),SURFACE,8)
    icon(d,'panel',right-41,173,ACCENT if evidence else MUTED,20)

def reader(d, x, right=1540, kind='笔记', title='笔记', body=None, tag=None):
    d.rectangle((x,217,right,959),fill='#171b21')
    d.rectangle((x,922,right,959),fill='#15191f')
    line(d,(x,922,right,922),EDGE)
    if kind == '笔记':
        txt(d,(x+63,270),'笔记   ·   版本 2',16,ACCENT)
        txt(d,(x+63,334),title,34,TEXT)
        line(d,(x+63,401,right-70,401),EDGE)
        txt(d,(x+63,439),'我喜欢编程，也喜欢研究新技术。',23,TEXT)
        txt(d,(x+63,499),'平时会把阅读和工作中的想法整理到这里。',23,TEXT)
        if tag:
            rr(d,(x+63,576,x+152,610),'#26352f',8)
            txt(d,(x+77,582),tag,16,GREEN)
    elif kind == '文件':
        txt(d,(x+63,270),'文件   ·   PDF',16,ACCENT)
        txt(d,(x+63,328),title,33,TEXT)
        line(d,(x+63,397,right-70,397),EDGE)
        txt(d,(x+63,446),'第一章  知识如何被重新组织',23,TEXT)
        txt(d,(x+63,507),'资料之间的连接，让碎片化的内容成为可持续使用的知识。',21,TEXT)
        txt(d,(x+63,558),'阅读时保留原文证据，之后可以追溯到每一次更新。',21,TEXT)
        txt(d,(right-118,938),'第 1 / 12 页',13,FAINT)
    else:
        txt(d,(x+63,270),'链接   ·   版本 1',16,ACCENT)
        txt(d,(x+63,331),title,30,TEXT)
        line(d,(x+63,397,right-70,397),EDGE)
        txt(d,(x+63,442),'https://my.feishu.cn/wiki/ObDWw96POid0...',19,TEXT)
        txt(d,(x+63,502),'已保存网址，等待内容识别。',18,MUTED)
    txt(d,(x+31,936),'已打开：'+title,13,FAINT)

def bottom_tree(d,left,right,style='full'):
    line(d,(left,754,right,754),EDGE)
    txt(d,(left+22,772),'知识目录',15,MUTED)
    txt(d,(right-33,771),'＋',22,MUTED)
    rr(d,(left+13,808,right-14,846),SURFACE,8)
    icon(d,'folder',left+27,818,MUTED,20)
    txt(d,(left+58,813),'MyKnow',17,TEXT)
    txt(d,(left+23,876),'目录始终固定在侧栏底部',13,FAINT)

def left_header(d,left,right):
    txt(d,(left+22,176),'阅读空间',25,TEXT)
    txt(d,(left+22,211),'reader@example.com',14,MUTED)
    rr(d,(right-57,176,right-18,215),SURFACE,9)
    icon(d,'search',right-48,185,TEXT,18)

def document_row(d,left,y,title,kind='doc',selected=False,secondary=None,green=False,width=326):
    if selected: rr(d,(left+13,y-7,left+width,y+36),'#3b332f',8)
    icon(d,kind,left+25,y+3,ACCENT if selected else MUTED,17)
    txt(d,(left+52,y),title,16,TEXT if selected else '#ccd1d5')
    if secondary: txt(d,(left+52,y+20),secondary,12,MUTED)
    if green: d.ellipse((left+width-23,y+8,left+width-11,y+20),outline=GREEN,width=2)

def search_overlay(base):
    shadow=Image.new('RGBA',(W,H),(0,0,0,0)); sd=ImageDraw.Draw(shadow)
    sd.rounded_rectangle((570,253,1270,652),radius=20,fill=(0,0,0,145))
    shadow=shadow.filter(ImageFilter.GaussianBlur(18)); base.alpha_composite(shadow)
    d=ImageDraw.Draw(base)
    rr(d,(552,235,1252,633),'#252a30',20,'#5b6570',2)
    rr(d,(572,252,728,302),'#363e46',10)
    txt(d,(590,264),'全局搜索',17,TEXT)
    line(d,(707,271,713,277,719,271),MUTED,2)
    line(d,(741,251,741,302),EDGE)
    txt(d,(759,259),'程序员',24,TEXT)
    txt(d,(1186,265),'清除',14,MUTED)
    line(d,(572,314,1232,314),EDGE)
    txt(d,(575,331),'相关内容  ·  3 条',14,MUTED)
    rr(d,(568,359,1237,433),'#35414a',10)
    icon(d,'doc',586,380,ACCENT,21)
    txt(d,(620,371),'笔记',18,TEXT)
    txt(d,(620,402),'我是一名程序员啊  ·  MyKnow',14,MUTED)
    txt(d,(1180,384),'打开',14,MUTED)
    icon(d,'doc',586,455,MUTED,21)
    txt(d,(620,448),'笔记',18,TEXT)
    txt(d,(620,478),'职业规划  ·  最近更新于 9 月 28 日',14,MUTED)
    icon(d,'link',586,528,MUTED,21)
    txt(d,(620,522),'https://my.feishu.cn/wiki/ObD...',18,TEXT)
    txt(d,(620,552),'链接  ·  网址原文',14,MUTED)
    line(d,(572,579,1232,579),EDGE)
    txt(d,(580,594),'Tab 切换范围     上下键选择     回车打开     Esc 关闭',13,MUTED)

def make_a():
    im=Image.new('RGBA',(W,H)); d=ImageDraw.Draw(im)
    chrome(d,'方案 A · 分段浏览','沿用现有侧栏结构；模式清晰、改造成本最低。')
    rail(d); d.rectangle((119,151,458,959),fill=SIDE)
    left_header(d,119,458)
    rr(d,(135,263,442,307),SURFACE,9)
    rr(d,(139,267,239,303),'#494038',7)
    txt(d,(168,275),'标题',16,TEXT);txt(d,(268,275),'日期',16,MUTED);txt(d,(368,275),'标签',16,MUTED)
    txt(d,(141,334),'按标题浏览',15,MUTED);txt(d,(415,331),'4',14,MUTED)
    document_row(d,119,374,'笔记',selected=True,secondary='我喜欢编程和 AI',green=True)
    document_row(d,119,441,'我是一名程序员啊')
    document_row(d,119,495,'贾晨-全栈.pdf')
    document_row(d,119,549,'my.feishu.cn/wiki/ObD…',kind='link')
    bottom_tree(d,119,458)
    toolbar(d,458); reader(d,458,title='笔记',tag='编程')
    search_overlay(im)
    im.convert('RGB').save(OUT/'scheme-a-segmented.png')

def make_b():
    im=Image.new('RGBA',(W,H));d=ImageDraw.Draw(im)
    chrome(d,'方案 B · Finder 式双栏','左侧切换浏览维度，中间列出内容；适合资料量持续增长。')
    rail(d)
    d.rectangle((119,151,291,959),fill=SIDE)
    txt(d,(144,180),'浏览',23,TEXT)
    rr(d,(131,244,279,286),SURFACE,9);icon(d,'lines',147,258,ACCENT,19);txt(d,(181,250),'标题',17,TEXT)
    rr(d,(131,293,279,335),'#293139',9);icon(d,'calendar',147,307,ACCENT,19);txt(d,(181,299),'录入日期',17,TEXT)
    rr(d,(131,342,279,384),'#293139',9);icon(d,'tag',147,356,ACCENT,19);txt(d,(181,348),'内容标签',17,TEXT)
    line(d,(131,754,279,754))
    txt(d,(145,773),'知识目录',15,MUTED)
    icon(d,'folder',145,820,MUTED,18);txt(d,(174,812),'MyKnow',17,TEXT)
    txt(d,(146,903),'reader@example.com',12,FAINT)
    d.rectangle((291,151,595,959),fill='#20262e')
    txt(d,(315,177),'录入日期',23,TEXT)
    rr(d,(540,170,578,209),SURFACE,9);icon(d,'search',549,179,TEXT,18)
    txt(d,(315,246),'今天   ·   9 月 29 日',16,ACCENT)
    document_row(d,291,291,'贾晨-全栈.pdf',selected=True,width=291)
    document_row(d,291,345,'笔记',secondary='我喜欢编程和 AI',width=291)
    txt(d,(315,429),'昨天   ·   9 月 28 日',16,MUTED)
    document_row(d,291,474,'my.feishu.cn/wiki/…',kind='link',width=291)
    txt(d,(315,557),'9 月 26 日',16,MUTED)
    document_row(d,291,602,'我是一名程序员啊',width=291)
    txt(d,(314,930),'共 4 条内容',13,FAINT)
    toolbar(d,595);reader(d,595,kind='文件',title='贾晨-全栈.pdf')
    im.convert('RGB').save(OUT/'scheme-b-finder.png')

def make_c():
    im=Image.new('RGBA',(W,H));d=ImageDraw.Draw(im)
    chrome(d,'方案 C · 阅读优先','用一个浏览菜单保持侧栏安静；正文获得更大空间。')
    rail(d);d.rectangle((119,151,414,959),fill=SIDE)
    left_header(d,119,414)
    rr(d,(134,262,399,314),SURFACE,10)
    icon(d,'tag',151,278,ACCENT,19);txt(d,(183,270),'按内容标签',18,TEXT)
    line(d,(368,284,375,291,382,284),MUTED,2)
    txt(d,(140,344),'所有标签',15,MUTED);txt(d,(369,341),'4',14,FAINT)
    rr(d,(135,382,398,426),'#3b332f',9)
    txt(d,(152,390),'# 编程',17,TEXT);txt(d,(363,393),'2',15,MUTED)
    txt(d,(152,451),'# 工作',17,'#cdd2d6');txt(d,(363,453),'1',15,MUTED)
    txt(d,(152,505),'# 阅读',17,'#cdd2d6');txt(d,(363,507),'1',15,MUTED)
    line(d,(135,574,398,574))
    txt(d,(140,591),'编程 · 2 条内容',14,ACCENT)
    document_row(d,119,631,'笔记',secondary='我喜欢编程和 AI',selected=True,width=283)
    document_row(d,119,696,'我是一名程序员啊',width=283)
    bottom_tree(d,119,414)
    toolbar(d,414);reader(d,414,title='笔记',tag='编程')
    im.convert('RGB').save(OUT/'scheme-c-reading.png')

make_a();make_b();make_c()
