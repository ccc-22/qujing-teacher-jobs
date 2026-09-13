# -*- coding: utf-8 -*-
"""
曲靖教师编制雷达 —— 每日抓取脚本
抓取官方人社局 + 聚合站的教师招聘公告，过滤出"有编制"的机会，生成 data.json 和 index.html
用法: python fetch.py
"""
import json
import os
import re
import ssl
import sys
import time
import io
import hashlib
import datetime
import urllib.request
import urllib.error
from html.parser import HTMLParser

BASE = os.path.dirname(os.path.abspath(__file__))
STORE_FILE = os.path.join(BASE, 'store.json')
DATA_FILE = os.path.join(BASE, 'data.json')
TEMPLATE_FILE = os.path.join(BASE, 'template.html')
INDEX_FILE = os.path.join(BASE, 'index.html')

TODAY = datetime.date.today()
MAX_NEW_DETAILS_PER_RUN = 200  # 每次运行最多抓取的详情页数量（礼貌抓取，一次性补齐存量）
MAX_NEW_ATTACHMENTS_PER_RUN = 50  # 每次运行最多解析的附件数量

# ---------------------------------------------------------------- 抓取基础

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def fetch(url, timeout=25):
    """抓取 URL，自动处理编码，返回 html 文本"""
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
    })
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        raw = r.read()
    m = re.search(rb'charset=["\']?([\w-]+)', raw[:4000], re.I)
    enc = m.group(1).decode() if m else 'utf-8'
    try:
        return raw.decode(enc, errors='replace')
    except LookupError:
        return raw.decode('utf-8', errors='replace')


class LinkParser(HTMLParser):
    """提取所有 <a href> 及其内部文本"""
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self._href = dict(attrs).get('href', '')
            self._buf = []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self._href is not None:
            text = re.sub(r'\s+', '', ''.join(self._buf))
            if text:
                self.links.append((self._href, text))
            self._href = None
            self._buf = []


def extract_text(html):
    """详情页 HTML -> 纯文本"""
    html = re.sub(r'<(script|style)[\s\S]*?</\1>', ' ', html, flags=re.I)
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'&nbsp;?', ' ', text)
    return re.sub(r'\s+', ' ', text)


# 华图/中公详情页正文容器（导航、上一篇/下一篇、延伸阅读在容器外）
RE_MAIN_CONTAINER = re.compile(
    r'<(?:div|section)[^>]*class=["\'][^"\']*?(?:detail-cnt|detail_content|article-content|articleCont|'
    r'conTxt|news-content|TRS_Editor|zxzx_content)[^"\']*?["\'][^>]*>', re.I)


def extract_main_text(html):
    """详情页 HTML -> 正文纯文本。
    优先取正文容器（华图系 detail-cnt 等），避免"上一篇/延伸阅读"里的其他公告标题
    干扰英语/负面词判断；政府站等无容器页面回退全页文本。
    """
    m = RE_MAIN_CONTAINER.search(html)
    if m:
        # 取容器起点到最近的配套闭合（简单按相同标签层级截断：取容器后 200KB 内内容）
        start = m.end()
        chunk = html[start:start + 200000]
        text = extract_text(chunk)
        # 截断到"上一篇/下一篇/延伸阅读"等分隔词
        for sep in ('上一篇', '下一篇', '延伸阅读', '相关推荐', '上一篇：', '下一篇：'):
            idx = text.find(sep)
            if idx > 1000:
                text = text[:idx]
                break
        if len(text) > 500:
            return text
    return extract_text(html)


def extract_real_title(html):
    """从详情页提取完整标题（聚合站列表页标题常被截断，导致流程公示漏判）"""
    raws = []
    m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S | re.I)
    if m:
        raws.append(m.group(1))
    m = re.search(r'<title>(.*?)</title>', html, re.S | re.I)
    if m:
        raws.append(m.group(1))
    for raw in raws:
        t = re.sub(r'<[^>]+>', '', raw)
        t = re.sub(r'\s+', '', t)
        t = re.split(r'[_｜|]', t)[0]     # 去掉 "_云南华图" 类下划线后缀
        prev = None
        while prev != t:                  # 去掉 "-曲靖市人力资源和社会保障局" 类横线后缀
            prev = t
            t = SITE_SUFFIX.sub('', t)
        t = t.strip(' -–—：:·')
        if 10 <= len(t) <= 120:
            return t
    return ''


def abs_url(href, base):
    """相对链接 -> 绝对链接"""
    if href.startswith(('http://', 'https://')):
        return href
    if href.startswith('//'):
        # 协议相对链接（如 //m.yn.offcn.com/...），补上当前页面的协议
        m = re.match(r'(https?)://', base)
        return (m.group(1) if m else 'https') + ':' + href
    if href.startswith('/'):
        m = re.match(r'(https?://[^/]+)', base)
        return m.group(1) + href if m else href
    if href.startswith('javascript') or not href:
        return None
    # 相对路径
    m = re.match(r'(https?://[^/]+)(/[^\?]*)?/', base)
    return base.rsplit('/', 1)[0] + '/' + href


# ---------------------------------------------------------------- 规则配置

# 地区关键词
RE_QUJING = re.compile(r'曲靖|麒麟|沾益|马龙|陆良|师宗|罗平|富源|会泽|宣威|云南能源职业技术学院')
RE_KUNMING = re.compile(r'昆明|云南师范大学|安宁|嵩明|宜良|石林|寻甸|禄劝|东川|富民|晋宁')
RE_YUXI = re.compile(r'玉溪|红塔|澄江[^西]|通海|江川|华宁|易门|峨山|新平|元江|玉溪师范学院')
RE_HONGHE = re.compile(r'红河|蒙自|个旧|开远|弥勒(?!市|镇)|建水|泸西|元阳|绿春|石屏|屏边|河口|金平')
RE_WENSHAN = re.compile(r'文山|砚山|西畴|麻栗坡|马关|丘北|广南|富宁')
RE_ZHAOTONG = re.compile(r'昭通|昭阳|鲁甸|巧家|彝良|威信|镇雄|大关|永善|绥江|水富')
RE_GUIZHOU_NEAR = re.compile(
    r'兴义|黔西南|六盘水|盘州|水城|兴仁|安龙|普安|贞丰|册亨|望谟|晴隆')
RE_GUIZHOU_OTHER = re.compile(r'贵州|贵阳|遵义|毕节|安顺|黔东南|黔南|铜仁|凯里|都匀')
RE_YUNNAN = re.compile(r'云南')
RE_GUIZHOU_FAR = re.compile(r'贵州|贵阳|遵义|毕节|安顺|黔东南|黔南|铜仁')
RE_OTHER_PROVINCE = re.compile(
    r'广东|广西|四川|重庆|湖南|湖北|河南|河北|山东|山西|陕西|甘肃|江苏|浙江|安徽|福建|江西|'
    r'海南|贵州|内蒙古|新疆|西藏|青海|宁夏|黑龙江|吉林|辽宁|天津|上海|北京')

RE_OPP = re.compile(r'(招聘|引进|特岗|选调|招募|考聘)')          # 机会类标题
RE_RESULT = re.compile(                                          # 结果/流程类标题（非机会）
    r'(成绩|资格复审|体检|考察|拟聘|拟录|拟进|名单|公示|更正|方案|通告|温馨提示|'
    r'工作安排|大纲|成绩查询|结果|递补|面试通知|面试公告|考核公告|考核通知|准考证|答复|回复|处理情况|选调)')
RE_INFO = re.compile(                                            # 资讯/问答类文章（非公告）
    r'(考什么|什么时候|怎么考|报名入口|查询时间|最新招聘信息|入面分数|分数线|多少分|'
    r'考试科目|科目有哪些|待遇|怎么样|难吗|备考|真题|答案|职位表下载|汇总$|时间安排|'
    r'报考指南|常见问题|招聘信息$|复盘|考后)')
RE_RESULT_BODY = re.compile(                                     # 正文级"流程/结果公告"特征（短语特异，普通公告不会出现）
    r'(拟聘（录）用人员|拟录（聘）用人员|拟聘用人员名单|拟录用人员名单|拟聘用人员公示|'
    r'拟聘人员名单|拟聘人员公示|拟录用人员公示|拟进入体检|'
    r'进入体检考察环节|体检、考察结果|体检考察结果|资格复审结果|综合成绩、资格复审|取消岗位情况)')
# 流程类公告的标题特征短语（如"资格复审公告"）——只在正文开头 400 字内匹配，
# 避免误伤正常招聘简章里的流程描述（简章里"面试名单"等词一般出现在 500 字之后）
RE_RESULT_BODY_EARLY = re.compile(
    r'(资格复审(?:人员名单|名单|公告|通知)(?!将|另行)|面试(?:人员名单|名单|公告|通知)(?!将|另行)|'
    r'体检(?:人员名单|名单|公告|通知)(?!将|另行)|考察(?:人员名单|公告|通知)(?!将|另行)|'
    r'拟聘用名单|拟聘名单|拟录用名单|名单公示|笔试成绩(?:公告|公示)|成绩公示|成绩公告|'
    r'递补名单|递补公告|递补人员)')
# 明确不招英语：其他学科岗位词（用于剔除与英语无关的招聘信息）
RE_OTHER_SUBJECT = re.compile(
    r'语文|数学|(?<!微)生物|物理|化学|地理|历史|体育|音乐|美术|思想政治|道德与法治|'
    r'心理健康|信息技术|学前教育|幼儿教师|幼儿园教师|科学教师|科学学科|政治(?!面貌|素质)')
RE_SUBJECT_POST = re.compile(
    r'(?:语文|数学|物理|化学|(?<!微)生物|地理|历史|体育|音乐|美术|科学|信息技术|思想政治|道德与法治|心理健康)\s*'
    r'(?:教师|老师|学科|岗|课程|[：:])')
RE_NOT_ENGLISH = re.compile(r'不含英语|不设英语|不招英语|英语学科除外|英语教师除外|除英语外|非英语学科')
RE_ENGLISH_WEAK = re.compile(r'(?:大学英语)?[四六]级|英语[四六]级')
SITE_SUFFIX = re.compile(                                        # 详情页 <title> 里的网站名后缀
    r'[-–—](?:曲靖市人力资源和社会保障局|云南省人力资源和社会保障厅|云南人事考试网|云南人事考试|'
    r'云南华图教育|云南华图|华图教育|华图教师网|山香教育|高校人才网|教师招聘网|曲靖市人民政府|'
    r'公务员考试网|事业单位招聘网)$')
STORE_VERSION = 11  # 分析规则版本：变更后存量条目会重新抓详情页复核（v11: 修复尾部推荐区污染导致的 neg/english 误杀——RE_NEG 去掉"聘用制"、"见习"收紧为"就业见习"，正文判断前截断推荐链接区）
RE_TEACHER = re.compile(
    r'教师|教育|学校|学院|幼儿园|师范|教体|教学|中学|小学|职校|技工学校')
RE_UNIT = re.compile(r'事业单位')  # 事业单位公开招聘（未必带"教师"字样，如统考公告）
# 注意："聘用制"是事业单位编制内标准表述（"实行聘用制"），真正的编外聘用岗必带
# "合同制/劳务派遣/编制外/编外"兜底词，故不作为负向词；
# "见习期"是编制内新聘试用期，仅"就业见习"（无编临时岗）算负向。
RE_NEG = re.compile(
    r'编制外|编外|合同制|劳动合同制|劳务派遣|人事代理|临聘|代课|顶岗|公益性岗位|'
    r'非全日制|辅助人员|政府购买|购买服务|非在编|第三方|民办|外包|就业见习')
RE_POS_STRONG = re.compile(
    r'事业编制|纳入编制|编制内|使用编制|特岗计划|特岗教师|公费师范|公费师范生|'
    r'公开引进|公开招聘|人才引进|事业单位公开招聘')
RE_ENGLISH = re.compile(r'英语')
RE_FRESH = re.compile(r'(?:^|[^往])应届[毕业]?(?:生|毕业生|研究生)|(?:面向|仅限)\s*20\d{2}届')
# 公费师范生/优师专项-only 招聘（仅招指定高校在校定向生，社会人员不可报，属身份限定无用信息）
RE_GONGFEI = re.compile(r'公费师范|公费教育师范|部属师范|优师计划|优师专项')
RE_AGE = re.compile(r'年龄[^。；，]{0,30}?(\d{2})\s*周岁')
RE_AGE_RANGE = re.compile(r'(\d{2})\s*周岁\s*[至到～~\-—]\s*(\d{2})\s*周岁')
RE_HEADCOUNT = re.compile(r'[（(]?(\d{1,4})\s*(?:名|人)[）)]?\s*[）)]?$')
RE_HEADCOUNT_BODY = re.compile(r'(?:公开招聘|招聘|引进)\S{0,12}?(\d{1,4})\s*(?:名|人)')
RE_DEADLINE_RANGE = re.compile(
    r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日[^。；]{0,12}?'
    r'(?:至|到|—|－|--|~|～|—)\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')
RE_DEADLINE_UNTIL = re.compile(
    r'(?:截至|截止(?:时间|日期)?)[：:为]?\s*(?:(\d{4})\s*年)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')
RE_PUBLISH_GOV = re.compile(r'发布时间[：:]\s*(\d{4})-(\d{1,2})-(\d{1,2})')

REGION_DETAIL_MAP = [
    (re.compile(r'麒麟'), '麒麟区'), (re.compile(r'沾益'), '沾益区'),
    (re.compile(r'马龙'), '马龙区'), (re.compile(r'陆良'), '陆良县'),
    (re.compile(r'师宗'), '师宗县'), (re.compile(r'罗平'), '罗平县'),
    (re.compile(r'富源'), '富源县'), (re.compile(r'会泽'), '会泽县'),
    (re.compile(r'宣威'), '宣威市'),
]

# 数据源定义：list_pages 为列表页地址（可含分页），force_region 强制地区
SOURCES = [
    {
        'key': 'qj_rsj_sydw', 'name': '曲靖市人社局', 'force_region': '曲靖',
        'pages': ['https://rsj.qj.gov.cn/list/sydw/auto/214.html',
                  'https://rsj.qj.gov.cn/list/sydw/2/214.html',
                  'https://rsj.qj.gov.cn/list/sydw/3/214.html'],
    },
    {
        'key': 'qj_rsj_gsgg', 'name': '曲靖市人社局', 'force_region': '曲靖',
        'pages': ['https://rsj.qj.gov.cn/list/gsgg/auto/108.html',
                  'https://rsj.qj.gov.cn/list/gsgg/2/108.html',
                  'https://rsj.qj.gov.cn/list/gsgg/3/108.html'],
    },
    {
        'key': 'yn_hrss', 'name': '云南省人社厅',
        'pages': ['https://hrss.yn.gov.cn/NewsLsit.aspx?ClassID=458',
                  'https://hrss.yn.gov.cn/NewsLsit.aspx?ClassID=602',
                  'https://hrss.yn.gov.cn/NewsLsit.aspx?ClassID=602&page=3',
                  'https://hrss.yn.gov.cn/NewsLsit.aspx?ClassID=602&page=4'],
    },
    {
        'key': 'huatu_jszp', 'name': '华图教育',
        'pages': ['https://yn.huatu.com/jiaoshi/kaoshi/jszp/'],
    },
    {
        'key': 'huatu_tg', 'name': '华图教育',
        'pages': ['https://yn.huatu.com/jiaoshi/tg/zkgg/'],
    },
    {
        'key': 'shanxiang_yunnan', 'name': '山香教育',
        'pages': ['https://www.shanxiangjiaoyu.com/zixun/20260127/78352'],
        'summary_page': True,  # 云南公告汇总文章页，解析内链
    },
    {
        'key': 'shanxiang_zkgg', 'name': '山香教育',
        'pages': ['https://www.shanxiangjiaoyu.com/jszp/zhaokao/zkgg'],
    },
    {
        'key': 'gz_huatu_jszp', 'name': '贵州华图',
        'pages': ['https://m.gz.huatu.com/list/jiaoshi/gonggao/'],
    },
    {
        'key': 'gz_huatu_tg', 'name': '贵州华图',
        'pages': ['https://m.gz.huatu.com/list/jiaoshi/gonggao/'],
    },
    {
        'key': 'hhzrc', 'name': '红河人才网',
        'pages': ['https://www.hhzrc.cn/Plus/Index/SinglePage/index/id/48'],
    },
    {
        'key': 'gz_hrss', 'name': '贵州人社厅',
        'pages': ['https://rst.guizhou.gov.cn/zwgk/zdlyxx/sydwgkzp/index.html',
                  'https://rst.guizhou.gov.cn/zwgk/zdlyxx/sydwgkzp/index_1.html'],
    },
    {
        'key': 'yn_huatu', 'name': '云南华图',
        'pages': ['https://m.yn.huatu.com/list/jiaoshi/kaoshi/jszp/'],
    },
    {
        'key': 'yn_offcn', 'name': '云南中公教育',
        'pages': ['https://m.yn.offcn.com/html/jiaoshi/zhaokaoxinxi/zpgg/'],
    },
    {
        'key': 'huatu_yn', 'name': '华图教师云南',
        'pages': ['https://m.huatu.com/kaoshi/jiaoshi/appks/yn'],
    },
]

# ---------------------------------------------------------------- 分类与解析


def classify_region(title):
    if RE_QUJING.search(title):
        detail = ''
        for pat, name in REGION_DETAIL_MAP:
            if pat.search(title):
                detail = name
                break
        return '曲靖', detail
    if RE_KUNMING.search(title):
        return '昆明', ''
    if RE_YUXI.search(title):
        return '玉溪', ''
    if RE_HONGHE.search(title):
        return '红河', ''
    if RE_WENSHAN.search(title):
        return '文山', ''
    if RE_ZHAOTONG.search(title):
        return '昭通', ''
    if RE_GUIZHOU_NEAR.search(title):
        return '贵州邻近', ''
    if RE_GUIZHOU_OTHER.search(title):
        return '贵州其他', ''
    if RE_YUNNAN.search(title):
        return '云南其他', ''
    return None, ''


def classify_type(title):
    if '特岗' in title:
        return '特岗'
    if '引进' in title:
        return '人才引进'
    if re.search(r'事业单位|D类|统考|分类考试', title):
        return '事业编统考'
    if RE_TEACHER.search(title):
        return '教师招聘'
    return '其他'


def parse_date_from_url(url):
    m = re.search(r'/(20\d{2})[/-]?(\d{2})[/-]?\d{0,2}/', url)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), 1).isoformat()
        except ValueError:
            pass
    return None


def clean_deadline(y, mo, d, ref_year):
    try:
        year = int(y) if y else ref_year
        dt = datetime.date(year, int(mo), int(d))
        # 跨年修正：如果解析出的日期比参考年早太多，视为下一年
        if dt.year < TODAY.year - 1:
            dt = dt.replace(year=TODAY.year)
        return dt
    except (ValueError, TypeError):
        return None


def parse_deadline(text, ref_year):
    m = RE_DEADLINE_RANGE.search(text)
    if m:
        return clean_deadline(m.group(1), m.group(4), m.group(5), ref_year)
    m = RE_DEADLINE_UNTIL.search(text)
    if m:
        return clean_deadline(m.group(1), m.group(2), m.group(3), ref_year)
    return None


def analyze_body(text):
    """分析详情页正文，返回 dict(age, deadline, english, neg_hit)"""
    info = {'age': '', 'deadline': None, 'english': False, 'neg_hit': False}
    if not text:
        return info
    m = RE_AGE_RANGE.search(text)
    if m:
        info['age'] = '≤%s周岁' % m.group(2)
    else:
        m = RE_AGE.search(text)
        if m:
            info['age'] = '≤%s周岁' % m.group(1)
    ref_year = TODAY.year
    info['deadline'] = parse_deadline(text, ref_year)
    # "大学英语四/六级"属于通用报考要求，不算英语学科信号（先抹掉再匹配）
    info['english'] = bool(RE_ENGLISH.search(RE_ENGLISH_WEAK.sub(' ', text)))
    info['neg_hit'] = bool(RE_NEG.search(text))
    return info


# 页面尾部/侧栏的站名与推荐链接特征：命中短语紧邻这些词，说明是混入正文的其他公告标题
RE_SITE_TAIL = re.compile(
    r'华图教育|公务员之路|山香教育|中公教育|相关推荐|延伸阅读|上一篇|下一篇|查看更多|点击咨询|在线客服')

# 页面尾部（相关推荐/客服/关注区）特征：neg/english 等正文判断前先截掉尾部。
# 聚合站 fallback 提取的全页文本会在正文后混入"相关推荐"等其他公告标题
# （含"编外聘用""聘用制""临聘""英语"等词），污染正文级判断（曾致 30+ 条正常简章误杀）。
RE_BODY_TAIL = re.compile(
    r'相关推荐|延伸阅读|上一篇|下一篇|有报考疑惑|在线客服|扫码关注|微信扫一扫|立即关注')


def trim_body_tail(text):
    """截断正文尾部推荐链接/客服区，只保留正文主体（从 1000 字后开始找，防误截正文开头）"""
    if not text:
        return text
    m = RE_BODY_TAIL.search(text, 1000)
    return text[:m.start()] if m else text

# 聚合站导航菜单词：连续出现 2 个以上即为菜单串（fallback 提取的全页文本会把菜单混在标题附近）
RE_NAV_TOKEN = (
    r'首页|教师招聘|招考公告|招教动态|成绩查询|面试公告|面试通知|考试动态|考试指南|学科知识|'
    r'公共基础|面试技巧|试题下载|特岗考试|特岗公告|报考指南|时政热点|真题试题|备考资料|视频课程|'
    r'面授课程|教师用书|招教快讯|幼师招聘|资格证考试|普通话考试|联系我们|关于我们|网站地图|'
    r'事考公告|基础知识|职业能力|综合应用|公基时政|笔试试题|面试试题|每日一练|教师资格|笔试公告|'
    r'资格认定|综合素质|知识能力|资料下载|每日测评')
RE_NAV_RUN = re.compile(
    r'(?:%s)(?:[\s／/|＞>·•]*(?:%s)){1,}' % (RE_NAV_TOKEN, RE_NAV_TOKEN))


def early_process_hit(text, title):
    """正文"标题式"流程短语检查（RE_RESULT_BODY_EARLY）。
    从正文中定位完整标题的位置，只检查标题之后 600 字，并先剥离导航菜单串——
    避免聚合站导航菜单（"首页 教师招聘 …成绩查询 面试公告 考试指南…"）误触发。
    """
    if not text:
        return False
    anchor = (title or '')[:15]
    idx = text.find(anchor) if anchor else -1
    head = text[idx:idx + 600] if idx >= 0 else text[:400]
    head = RE_NAV_RUN.sub(' ', head)
    return bool(RE_RESULT_BODY_EARLY.search(head))


def body_process_hit(text):
    """正文级流程短语检查（RE_RESULT_BODY），带误报防护：
    - 命中点紧跟"等/将/在/于"等（简章对未来公示的预告句，如"拟聘用人员名单将在公众号公示5个工作日"）→ 跳过
    - 命中点前后紧邻站名/推荐链接（相关推荐里混入的其他公告标题）→ 跳过
    """
    if not text:
        return False
    for m in RE_RESULT_BODY.finditer(text[:2000]):
        after = text[m.end():m.end() + 50]
        # 紧跟"等/将/在/于"等为简章预告句
        if re.match(r'\s*(?:等|将|在|于|适时|另行)', after):
            continue
        before = text[max(0, m.start() - 50):m.start()]
        if RE_SITE_TAIL.search(before) or RE_SITE_TAIL.search(after):
            continue
        return True
    return False


# ---------------------------------------------------------------- 岗位表附件解析

RE_ATTACH_ENGLISH = re.compile(r'英语|外国语言文学|外国语|英语教师|英语专业|英语学科|外语教学|英语语言文学')
RE_ATTACH_PLAN = re.compile(
    r'计划表|岗位表|招聘计划|职位表|岗位一览表|公开招聘人员|岗位信息表|招聘岗位表|岗位明细')
# 参考材料关键词（专业目录、考试大纲等，不是岗位表，不应被解析）
RE_ATTACH_REFERENCE = re.compile(
    r'专业目录|专业代码|考试大纲|笔试大纲|考试类别|学科专业')


def find_attachments(html, base_url):
    """从详情页 HTML 中提取附件链接，返回 [(url, type), ...
    type: 'xlsx' | 'xls' | 'pdf'
    只返回文件名包含"岗位表"、"计划表"等关键词的附件（排除专业目录、考试大纲等参考材料）
    """
    links = []
    for m in re.finditer(
            r'<a[^>]*href=["\']([^"\']*\.(xlsx?|pdf))["\'][^>]*>(.*?)</a>',
            html, re.I | re.S):
        href = m.group(1).strip()
        ext = m.group(2).lower()
        text = re.sub(r'<[^>]+>', '', m.group(3)).strip()
        # 只保留文件名包含"计划表"、"岗位表"等关键词的附件
        if not RE_ATTACH_PLAN.search(text + href):
            continue
        # 排除专业目录、考试大纲等参考材料
        if RE_ATTACH_REFERENCE.search(text + href):
            continue
        if not href.startswith('http'):
            # 相对路径转绝对
            if href.startswith('/'):
                m2 = re.match(r'(https?://[^/]+)', base_url)
                href = m2.group(1) + href if m2 else href
            else:
                href = base_url.rsplit('/', 1)[0] + '/' + href
        links.append((href, ext))
    return links


def extract_xlsx_text(url):
    """下载并解析 XLSX 文件，返回文本"""
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': UA,
            'Accept': 'application/octet-stream,*/*',
        })
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
            data = r.read()
        if len(data) < 50:
            return ''
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        parts = []
        for sheet in wb.worksheets:
            for row in sheet.iter_rows(values_only=True):
                for cell in row:
                    if cell is not None:
                        parts.append(str(cell))
        wb.close()
        return ' '.join(parts)
    except Exception:
        return ''


def extract_xls_text(url):
    """下载并解析旧版 XLS 文件，返回文本（使用 xlrd）"""
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': UA,
            'Accept': 'application/octet-stream,*/*',
        })
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
            data = r.read()
        if len(data) < 50:
            return ''
        import xlrd
        wb = xlrd.open_workbook(file_contents=data)
        parts = []
        for sheet in wb.sheets():
            for row in range(sheet.nrows):
                for cell in sheet.row_values(row):
                    if cell:
                        parts.append(str(cell))
        return ' '.join(parts)
    except Exception:
        return ''


def extract_pdf_text(url):
    """下载并解析 PDF 文件，返回纯文本。
    支持两种 PDF：
    1) 文本型 PDF：直接用 pymupdf 提取文字，速度快
    2) 扫描件/图片型 PDF：text 提取为空，自动回退到 OCR（pytesseract + chi_sim+eng）
    """
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
            data = r.read()
        if len(data) < 100:
            return ''
        import pymupdf
        doc = pymupdf.open(stream=data, filetype='pdf')
        # 尝试提取文本
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        text = ''.join(text_parts)
        # 如果文本太少（<50 字符），可能是扫描件，回退到 OCR
        if len(text.strip()) < 50:
            try:
                import pytesseract
                from PIL import Image
                import io
                # 设置 pytesseract 路径（Tesseract 可能不在 PATH 中）
                tess_exe = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
                if os.path.exists(tess_exe):
                    pytesseract.pytesseract.tesseract_cmd = tess_exe
                # 设置 tessdata 优先路径（用户目录下的中文包）
                user_tessdata = os.path.join(os.path.expanduser('~'), '.tesseract', 'tessdata')
                if os.path.isdir(user_tessdata):
                    os.environ['TESSDATA_PREFIX'] = user_tessdata
                ocr_parts = []
                for page in doc:
                    pix = page.get_pixmap(dpi=300)
                    img = Image.open(io.BytesIO(pix.tobytes('png')))
                    page_text = pytesseract.image_to_string(
                        img, lang='chi_sim+eng',
                        config='--psm 6 --oem 3').strip()
                    if page_text:
                        ocr_parts.append(page_text)
                doc.close()
                ocr_text = '\n'.join(ocr_parts)
                # 后处理：去掉中文字符间的空格（OCR 常见问题，如"英 语"→"英语"）
                ocr_text = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', ocr_text)
                if len(ocr_text.strip()) > 50:
                    return ocr_text
            except Exception:
                pass  # OCR 失败时回退到 pymupdf 的文本结果
        doc.close()
        return text
    except Exception:
        return ''


def make_id(url):
    return hashlib.md5(url.encode('utf-8')).hexdigest()[:12]


# 聚合站域名：追原文时跳过这些站自身的链接
RE_AGG_DOMAIN = re.compile(r'huatu\.com|offcn\.com|shanxiangjiaoyu\.com')

# 附件后缀：公告里的岗位表/专业目录等附件不是"原文页"，不能作为原文链接返回
# （曾有山香详情页把正文引用的《全国技工院校专业目录》PDF 当成文山州公告原文）
RE_ATTACH_URL = re.compile(r'\.(pdf|xls|xlsx|xlsm|doc|docx|zip|rar|7z|wps)(?:[?#]|$)', re.I)


def find_origin_url(html):
    """从聚合站详情页提取"文章来源"等原文链接（公众号/官方公告）。
    华图/中公/山香只转载公告摘要，正文里一般带"文章来源: https://mp.weixin.qq.com/..."，
    顺着原文才能看到完整岗位明细。
    """
    # 1) "来源/文章来源/原文链接/转载自" 关键字后的 URL
    # 注意：不能用 \b 做 word boundary，因为 Python 3 re 的 \b 是 Unicode 感知的，
    # 中文汉字（如"来源"的"源"）被认为是 \w 字符，导致"源"和"h(ttps)"之间无边界。
    for m in re.finditer(
            r'(?:来源|文章来源|原文链接|公告来源|转载自)[^"\'<>]{0,20}?(https?://[^\s<>"\']+)',
            html, re.I):
        u = m.group(1).replace('&amp;', '&').strip()
        if RE_AGG_DOMAIN.search(u):
            continue
        # 附件（岗位表/专业目录等）不是公告原文页
        if RE_ATTACH_URL.search(u):
            continue
        # 排除 ICP 备案链接
        if re.search(r'beian\.', u, re.I):
            continue
        # 排除纯根域名（如 https://www.hh.gov.cn 无具体路径）
        parsed = u.split('://', 1)[-1] if '://' in u else u
        domain_path = parsed.split('/', 1)
        if len(domain_path) == 1 or len(domain_path[1]) <= 1:
            continue
        return u
    # 2) 兜底：正文里的公众号文章链接
    for m in re.finditer(r'https?://mp\.weixin\.qq\.com/s[^\s<>"\']+', html):
        return m.group(0).replace('&amp;', '&').strip()
    # 3) 再兜底：提取正文中的 gov.cn 链接（华图教育网正文里常有 "hrss.yn.gov.cn" 但无"来源"前缀）
    for m in re.finditer(
            r'https?://[^\s<>"\'，。、；）\))]*?\.gov\.cn[^\s<>"\'，。、；）\))]*',
            html):
        u = m.group(0).replace('&amp;', '&').strip()
        # 排除备案号、样式表等非正文链接
        if re.search(r'beian\.|\.(css|js|png|jpg|gif|ico|svg)$', u):
            continue
        # 附件（岗位表/专业目录等）不是公告原文页
        if RE_ATTACH_URL.search(u):
            continue
        if '/Scripts/' in u or '/styles/' in u:
            continue
        # 排除常见的非原文链接
        if re.search(r'hrss\.yn\.gov\.cn/ynrsksw/Index|zwfw\.yn\.gov\.cn|yuxi\.gov\.cn', u):
            continue
        # 只接受路径长度 > 20 的 URL（排除域名根目录如 hrss.yn.gov.cn/）
        path = u.split('.gov.cn', 1)[-1] if '.gov.cn' in u else ''
        if len(path) <= 1:
            continue
        return u
    # 4) 最后兜底：提取正文中的学校/教育局官网链接（非聚合站域名）
    #    排除常见的非原文链接（如 cscse.edu.cn 留学服务中心、jszg.edu.cn 教师资格网等）
    RE_BAD_ORIGIN = re.compile(
        r'cscse\.edu\.cn|jszg\.edu\.cn|hrss\.yn\.gov\.cn/ynrsksw/Index|'
        r'zwfw\.yn\.gov\.cn|yuxi\.gov\.cn|beian\.')
    school_domains = re.findall(
        r'https?://(?:[\w-]+\.)*(?:edu\.cn|ynmzzx|ynmdfz|ynqfzyxy|ynu\.edu|rsc\.\w+\.edu)[^\s<>"\'，。、；）\))]*',
        html)
    for u in school_domains:
        u = u.replace('&amp;', '&').strip()
        if not re.search(r'\.(css|js|png|jpg|gif|ico|svg)$', u) and not RE_BAD_ORIGIN.search(u):
            return u
    return ''


def parse_attachment_english(att_url, att_type):
    """解析单个附件（xlsx/xls/pdf），返回是否含英语关键词"""
    try:
        if att_type == 'xlsx':
            att_text = extract_xlsx_text(att_url)
        elif att_type == 'xls':
            att_text = extract_xls_text(att_url)
        else:
            att_text = extract_pdf_text(att_url)
        return bool(att_text) and bool(RE_ATTACH_ENGLISH.search(att_text))
    except Exception:
        return False


# ---------------------------------------------------------------- 主流程


def collect_candidates(src):
    """抓取一个数据源的所有列表页，返回候选 {url: title}"""
    found = {}
    ok_pages = 0
    for page_url in src['pages']:
        try:
            html = fetch(page_url)
            ok_pages += 1
        except Exception as e:
            print('  [%s] 列表页失败 %s: %s' % (src['key'], page_url, e))
            continue
        p = LinkParser()
        p.feed(html)
        for href, text in p.links:
            url = abs_url(href, page_url)
            if not url or 'javascript' in url:
                continue
            title = text
            title = re.sub(r'^20\d{2}-\d{2}-\d{2}', '', title)   # 去掉前缀日期
            title = re.sub(r'^[推荐热顶新\[\]【】\s]+', '', title)  # 去掉标记前缀
            if len(title) < 10:
                continue
            if not RE_OPP.search(title):
                continue
            if RE_RESULT.search(title) or RE_INFO.search(title):
                continue
            found[url] = title
    return found, ok_pages


def main():
    print('=== 曲靖教师编制雷达 · 抓取开始 %s ===' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    # 载入存量
    store = {}
    if os.path.exists(STORE_FILE):
        with open(STORE_FILE, 'r', encoding='utf-8') as f:
            store = json.load(f)
    print('存量记录: %d 条' % len(store))

    # 1. 收集候选
    candidates = {}   # url -> {title, source_name, source_key, force_region, force_type}
    src_status = []
    for src in SOURCES:
        found, ok_pages = collect_candidates(src)
        for url, title in found.items():
            if url not in candidates:
                candidates[url] = {
                    'title': title, 'source': src['name'], 'source_key': src['key'],
                    'force_region': src.get('force_region'),
                }
        src_status.append({'name': src['name'], 'ok': ok_pages > 0,
                           'pages_ok': ok_pages, 'found': len(found)})
        print('[%s] 候选 %d 条' % (src['key'], len(found)))
    print('合计候选(去重前): %d 条' % len(candidates))

    # 2. 逐条处理
    jobs = []
    process = []          # 流程/结果公示：不进主列表，单独入口展示
    excluded = 0
    subject_filtered = 0
    new_details = 0
    new_attachments = 0

    # 2.0 存量补齐：优先抓取"已收录但从未分析详情/从未解析附件"的条目（学科未注明优先），
    #     避免新公告一直抢占详情页配额，导致老条目永远无法确认是否含英语。
    #     旧版曾把 analyzed=True 标记到附件解析功能上线之前，靠 attachment_has_english=None 兜底重抓。
    backfill = []
    for url, v in store.items():
        if v.get('status') == 'included' and (not v.get('analyzed') or v.get('attachment_has_english') is None):
            pri = 0 if v.get('subject_class') == 'teacher_unknown' else 1
            backfill.append((pri, url, v))
    backfill.sort(key=lambda x: x[0])
    for pri, url, old in backfill:
        if new_details >= MAX_NEW_DETAILS_PER_RUN:
            break
        try:
            raw_html = fetch(url, timeout=20)
            body_text = trim_body_tail(extract_main_text(raw_html)[:20000])
            new_details += 1
            attachment_has_english = False
            origin_url = ''
            if new_attachments < MAX_NEW_ATTACHMENTS_PER_RUN:
                attachments = find_attachments(raw_html, url)
                for att_url, att_type in attachments[:3]:  # 最多解析前3个
                    if parse_attachment_english(att_url, att_type):
                        attachment_has_english = True
                        new_attachments += 1
                        break
            # 聚合站正文一般没有岗位表，追原文补充正文和附件
            # 同时记录原文链接，供用户直接跳转（无需经过聚合站）
            # 政府来源（人社局/人才网）本身就是原文，不需要追原文链接
            is_gov_source = old.get('source_key') in {'qj_rsj_sydw', 'qj_rsj_gsgg', 'yn_hrss', 'hhzrc', 'gz_hrss'}
            if not attachment_has_english and not is_gov_source:
                origin = find_origin_url(raw_html)
                if origin:
                    origin_url = origin
                    try:
                        o_html = fetch(origin, timeout=20)
                        o_text = trim_body_tail(extract_main_text(o_html)[:20000])
                        if len(o_text) > len(body_text):
                            body_text = o_text
                        if new_attachments < MAX_NEW_ATTACHMENTS_PER_RUN:
                            o_atts = find_attachments(o_html, origin)
                            for att_url, att_type in o_atts[:3]:
                                if parse_attachment_english(att_url, att_type):
                                    attachment_has_english = True
                                    new_attachments += 1
                                    break
                        time.sleep(0.4)
                    except Exception:
                        pass
            # 即使 attachment_has_english 为 True 也尝试找原文链接（用于跳转）
            if not origin_url and not is_gov_source:
                origin = find_origin_url(raw_html)
                if origin:
                    origin_url = origin
            # 用新抓的正文/附件重算英语相关字段
            body_info = analyze_body(body_text)
            title = old.get('title', '')
            english_clear = bool(RE_ENGLISH.search(title)) or (attachment_has_english is True)
            english_maybe = body_info['english'] and not english_clear
            if english_clear:
                sc = 'english_clear'
            elif english_maybe:
                sc = 'english_maybe'
            else:
                sc = old.get('subject_class') or 'teacher_unknown'
            store[url].update({
                'body_text': body_text[:8000], 'analyzed': True, 'v': STORE_VERSION,
                'attachment_has_english': attachment_has_english,
                'origin_url': origin_url,
                'english': english_clear or english_maybe,
                'subject_class': sc,
            })
            time.sleep(0.4)
        except Exception as e:
            print('  [backfill fail] %s: %s' % (old.get('title', '?')[:30], e))
    if backfill:
        print('存量补齐: 目标 %d 条，实际抓取 %d 页' % (len(backfill), new_details))

    # 2.0b 存量规则迁移：用已存正文按新规则复核（无需网络）——
    # 流程/结果公告 → process；明确只招其他学科/企业招聘 → 剔除
    n_mig_process = 0
    n_mig_subject = 0
    n_mig_topic = 0
    for url, v in store.items():
        if v.get('status') != 'included':
            continue
        t = v.get('title', '')
        bt = v.get('body_text', '')
        if body_process_hit(bt) or early_process_hit(bt, t):
            v['status'] = 'process'
            n_mig_process += 1
            continue
        if re.search(r'有限公司|股份公司', t) or '科研助理' in t:
            v['status'] = 'excluded_topic'
            n_mig_topic += 1
            continue
        if not v.get('english') and v.get('attachment_has_english') is not True \
                and v.get('attachment_has_english') is not None:
            scope = t + ' ' + bt[:6000]
            if RE_NOT_ENGLISH.search(scope) or RE_OTHER_SUBJECT.search(t) or RE_SUBJECT_POST.search(scope):
                v['status'] = 'excluded_subject'
                n_mig_subject += 1
    if n_mig_process or n_mig_subject or n_mig_topic:
        print('存量迁移: 流程公示 %d 条，其他学科剔除 %d 条，企业/科研助理剔除 %d 条'
              % (n_mig_process, n_mig_subject, n_mig_topic))

    for url, cand in candidates.items():
        title = cand['title']

        # 存量里是否已有该 URL 的分析结果（规则版本变了就重抓复核；附件从未解析过的也重抓）
        item_id = make_id(url)
        old = store.get(url)
        need_detail = old is None or old.get('v') != STORE_VERSION or not old.get('analyzed') \
            or old.get('attachment_has_english') is None

        body_text = old.get('body_text', '') if old else ''
        raw_html = ''
        attachment_has_english = old.get('attachment_has_english') if old else None
        origin_url = old.get('origin_url', '') if old else ''
        refetched = False   # 本次是否真的重新抓取了详情页（配额满跳过时保持旧状态，下次继续）
        if need_detail and new_details < MAX_NEW_DETAILS_PER_RUN:
            try:
                raw_html = fetch(url, timeout=20)
                body_text = extract_main_text(raw_html)[:20000]
                new_details += 1
                refetched = True
                attachment_has_english = False
                origin_url = ''
                # 尝试解析附件（岗位表）
                if new_attachments < MAX_NEW_ATTACHMENTS_PER_RUN:
                    attachments = find_attachments(raw_html, url)
                    for att_url, att_type in attachments[:3]:  # 最多解析前3个
                        if parse_attachment_english(att_url, att_type):
                            attachment_has_english = True
                            new_attachments += 1
                            break
                # 聚合站正文一般没有岗位表，追原文（公众号/官方）补充正文和附件
                # 同时记录原文链接，供用户直接跳转
                # 政府来源（人社局/人才网）本身就是原文，不需要追原文链接
                is_gov_source = cand.get('source_key') in {'qj_rsj_sydw', 'qj_rsj_gsgg', 'yn_hrss', 'hhzrc', 'gz_hrss'}
                if not attachment_has_english and not is_gov_source:
                    origin = find_origin_url(raw_html)
                    if origin:
                        origin_url = origin
                        try:
                            o_html = fetch(origin, timeout=20)
                            o_text = trim_body_tail(extract_main_text(o_html)[:20000])
                            if len(o_text) > len(body_text):
                                body_text = o_text
                            if new_attachments < MAX_NEW_ATTACHMENTS_PER_RUN:
                                o_atts = find_attachments(o_html, origin)
                                for att_url, att_type in o_atts[:3]:
                                    if parse_attachment_english(att_url, att_type):
                                        attachment_has_english = True
                                        new_attachments += 1
                                        break
                            time.sleep(0.4)
                        except Exception:
                            pass
                # 即使 attachment_has_english 为 True 也尝试找原文链接（用于跳转）
                if not origin_url and not is_gov_source:
                    origin = find_origin_url(raw_html)
                    if origin:
                        origin_url = origin
                time.sleep(0.4)
            except Exception as e:
                print('  [detail fail] %s: %s' % (title[:30], e))
                body_text = old.get('body_text', '') if old else ''

        # 截断正文尾部推荐链接区：防止其他公告标题（"编外聘用""聘用制"等）污染后续正文判断
        body_text = trim_body_tail(body_text)

        # 详情页真实标题覆盖列表页截断标题（根治"……公示"被截断漏判的问题）
        if raw_html:
            real_title = extract_real_title(raw_html)
            if real_title and len(real_title) >= len(title):
                title = real_title

        # 地区判定（用尽量完整的标题）
        if cand['force_region']:
            region, region_detail = cand['force_region'], ''
            rd = classify_region(title)
            if rd[1]:
                region_detail = rd[1]
        else:
            region, region_detail = classify_region(title)
        if not region:
            if old:
                store[url]['status'] = 'excluded_region'
            excluded += 1
            continue

        # 教师/事业单位相关性（防止企业招聘、医疗卫生等混入）
        if not (RE_TEACHER.search(title) or RE_UNIT.search(title) or '特岗' in title):
            if old:
                store[url]['status'] = 'excluded_topic'
            excluded += 1
            continue

        # 企业/公司招聘（就业服务中心的劳务市场信息，如"XX教育科技集团有限公司招聘"）
        # 和科研助理岗（临时性研究岗位）：都不是教师编制机会
        if re.search(r'有限公司|股份公司', title) or '科研助理' in title:
            if old:
                store[url]['status'] = 'excluded_topic'
            excluded += 1
            continue

        # 研究生/硕士/博士-only 招聘排除（本科生不可报）
        grad_match = bool(re.search(r'[（(]硕士[）)]', title)) or \
                     bool(re.search(r'[（(]硕士[）)]', cand['title'])) or \
                     bool(re.search(r'公开招聘(?:研究生|硕士|博士)', title)) or \
                     bool(re.search(r'公开招聘(?:研究生|硕士|博士)', cand['title']))
        # 正文中明确说明仅招硕士/博士（且不招本科）的也排除
        if not grad_match and body_text:
            if re.search(r'[（(]硕士[）)]', body_text[:2000]) and \
               not re.search(r'本科|学士|师范', body_text[:2000]):
                grad_match = True
        if grad_match:
            if old:
                store[url]['status'] = 'excluded_grad'
            excluded += 1
            continue

        # 应届生-only 招聘排除（用户22年毕业，非应届）
        if re.search(RE_FRESH, title):
            if old:
                store[url]['status'] = 'excluded_fresh'
            excluded += 1
            continue

        # 公费师范生/优师专项-only 招聘排除（仅招在校定向生，社会人员不可报）
        if RE_GONGFEI.search(title):
            if old:
                store[url]['status'] = 'excluded_gongfei'
            excluded += 1
            continue

        jtype = classify_type(title)

        # 流程/结果公示判定：完整标题 + 正文前段双保险
        # 第三保险：正文标题后的标题式流程短语（如"资格复审公告"），
        # 弥补聚合站标题截断（"…名单公示"被截成"招聘"字样）导致的漏判
        is_process = bool(RE_RESULT.search(title)) or \
            body_process_hit(body_text) or \
            early_process_hit(body_text, title)
        if is_process:
            published = old.get('published') if old else None
            if not published:
                m = RE_PUBLISH_GOV.search(body_text)
                if m:
                    try:
                        published = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
                    except ValueError:
                        published = None
            if not published:
                published = parse_date_from_url(url) or TODAY.isoformat()
            process.append({
                'title': title, 'url': url, 'source': cand['source'],
                'region': region, 'region_detail': region_detail,
                'published': published,
                'first_seen': (old or {}).get('first_seen') or TODAY.isoformat(),
            })
            if not old:
                store[url] = {}
            store[url].update({
                'title': title, 'status': 'process', 'source': cand['source'],
                'region': region, 'region_detail': region_detail,
                'published': published,
                'analyzed': True if refetched else bool((old or {}).get('analyzed')),
                'v': STORE_VERSION if refetched else (old or {}).get('v', 0),
            })
            if body_text and refetched:
                store[url]['body_text'] = body_text[:8000]
            continue

        body_info = analyze_body(body_text)

        # 编制判断：先负面后正面
        neg = bool(RE_NEG.search(title)) or body_info['neg_hit']
        pos = bool(RE_POS_STRONG.search(title) or RE_POS_STRONG.search(body_text))
        if neg:
            store[url] = {
                'title': title, 'status': 'excluded_neg', 'source': cand['source'],
                'analyzed': True, 'v': STORE_VERSION, 'body_text': body_text[:5000],
                'first_seen': (old or {}).get('first_seen', TODAY.isoformat()),
            }
            excluded += 1
            continue
        if not old:
            store[url] = {}

        bianzhi = 'yes' if pos else 'check'
        english_clear = bool(RE_ENGLISH.search(title)) or (attachment_has_english is True)
        english_maybe = body_info['english'] and not english_clear
        english = english_clear or english_maybe

        # 明确不招英语/只招其他学科且无任何英语信号 → 剔除（无用信息，不进主列表）。
        # 标题宽松匹配（其他学科词），标题+正文用"学科+教师/岗"组合防误伤（如"办学历史"）；
        # 附件尚未解析的（attachment_has_english=None）暂不剔除，等重抓复核后再判。
        if not english and attachment_has_english is not None:
            scope = title + ' ' + body_text[:6000]
            if RE_NOT_ENGLISH.search(scope) or RE_OTHER_SUBJECT.search(title) or \
                    RE_SUBJECT_POST.search(scope):
                store[url].update({
                    'title': title, 'status': 'excluded_subject', 'source': cand['source'],
                    'analyzed': True, 'v': STORE_VERSION,
                    'body_text': body_text[:5000],
                    'first_seen': (old or {}).get('first_seen', TODAY.isoformat()),
                })
                subject_filtered += 1
                excluded += 1
                continue
        if english_clear:
            subject_class = 'english_clear'
        elif english_maybe:
            subject_class = 'english_maybe'
        elif jtype == '事业编统考':
            subject_class = 'general'
        else:
            subject_class = 'teacher_unknown'

        store[url].update({
            'title': title, 'status': 'included', 'source': cand['source'],
            'region': region, 'region_detail': region_detail, 'jtype': jtype,
            'subject_class': subject_class, 'english': english,
            # 只有真正重新抓取过详情页才推进版本/已分析标记，否则保持旧值下次继续
            'attachment_has_english': attachment_has_english if refetched else (old or {}).get('attachment_has_english'),
            'origin_url': origin_url if refetched else (old or {}).get('origin_url', ''),
            'analyzed': True if refetched else bool((old or {}).get('analyzed')),
            'v': STORE_VERSION if refetched else (old or {}).get('v', 0),
        })
        headcount = None
        m = RE_HEADCOUNT.search(title)
        if m:
            headcount = int(m.group(1))
        else:
            m = RE_HEADCOUNT_BODY.search(body_text)
            if m:
                headcount = int(m.group(1))

        published = old.get('published') if old else None
        if not published:
            m = RE_PUBLISH_GOV.search(body_text)
            if m:
                published = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
            else:
                published = parse_date_from_url(url)
        if not published:
            published = TODAY.isoformat()

        deadline = body_info['deadline']
        # 报名已截止的不再展示
        if deadline and deadline <= TODAY - datetime.timedelta(days=1):
            store[url]['status'] = 'expired'
            excluded += 1
            continue
        job = {
            'id': item_id,
            'title': title,
            'url': url,
            'origin_url': origin_url,
            'source': cand['source'],
            'region': region,
            'region_detail': region_detail,
            'jtype': jtype,
            'english': english,
            'subject_class': subject_class,
            'bianzhi': bianzhi,
            'age': body_info['age'],
            'headcount': headcount,
            'deadline': deadline.isoformat() if deadline else None,
            'published': published,
            'first_seen': (old or {}).get('first_seen') or TODAY.isoformat(),
        }
        jobs.append(job)
        # 完整存入 store，供下次"列表里暂时消失"的条目回填
        store[url].update({k: job[k] for k in job if k != 'id'})
        if body_text and refetched:
            store[url]['body_text'] = body_text[:8000]

    # 3. 合并存量中之前收录、本次列表里没出现的机会（可能还在报名期内），60天前的一律清理
    seen_urls = {j['url'] for j in jobs}
    process_urls = {p['url'] for p in process}
    cutoff = (TODAY - datetime.timedelta(days=60)).isoformat()
    stale = []
    for url, old in store.items():
        if url in seen_urls or url in process_urls:
            continue
        t = old.get('title', '')

        # 流程公示：保留近60天的，供独立入口查看
        if old.get('status') == 'process':
            if (old.get('published') or old.get('first_seen', '')) < cutoff:
                stale.append(url)
                continue
            process.append({
                'title': t, 'url': url, 'source': old.get('source', ''),
                'region': old.get('region', ''), 'region_detail': old.get('region_detail', ''),
                'published': old.get('published') or old.get('first_seen', TODAY.isoformat()),
                'first_seen': old.get('first_seen', TODAY.isoformat()),
            })
            continue

        if old.get('status') != 'included':
            continue
        if RE_RESULT.search(t) or RE_INFO.search(t):
            stale.append(url)
            continue
        if RE_NEG.search(t):
            stale.append(url)
            continue
        # 企业/科研助理排除（旧存量回流清理）
        if re.search(r'有限公司|股份公司', t) or '科研助理' in t:
            stale.append(url)
            continue
        # 正文级流程特征复核（规则上线前被误判为"收录"的流程公告，列表消失时回流）
        bt = old.get('body_text', '')
        if body_process_hit(bt) or early_process_hit(bt, t):
            store[url]['status'] = 'process'
            if (old.get('published') or old.get('first_seen', '')) >= cutoff:
                process.append({
                    'title': t, 'url': url, 'source': old.get('source', ''),
                    'region': old.get('region', ''), 'region_detail': old.get('region_detail', ''),
                    'published': old.get('published') or old.get('first_seen', TODAY.isoformat()),
                    'first_seen': old.get('first_seen', TODAY.isoformat()),
                })
            else:
                stale.append(url)
            continue
        # 明确不招英语/只招其他学科且无英语信号 → 剔除（旧存量回流清理）
        if not old.get('english') and old.get('attachment_has_english') is not True \
                and old.get('attachment_has_english') is not None:
            scope = t + ' ' + bt[:6000]
            if RE_NOT_ENGLISH.search(scope) or RE_OTHER_SUBJECT.search(t) or RE_SUBJECT_POST.search(scope):
                stale.append(url)
                continue
        # 研究生-only 排除（旧存量回流清理）
        if re.search(r'[（(]硕士[）)]', t) or re.search(r'公开招聘(?:研究生|硕士|博士)', t):
            stale.append(url)
            continue
        if (old.get('published') or old.get('first_seen', '')) < cutoff:
            stale.append(url)
            continue
        dl = old.get('deadline')
        if dl and datetime.date.fromisoformat(dl) <= TODAY - datetime.timedelta(days=1):
            stale.append(url)
            continue
        sc = old.get('subject_class')
        if not sc or sc == 'english' or old.get('attachment_has_english') is True:
            # 用新分类重算旧数据
            old_english = old.get('english', False)
            old_attach = old.get('attachment_has_english')
            title_has_eng = bool(RE_ENGLISH.search(t))
            if title_has_eng or old_attach is True:
                sc = 'english_clear'
            elif old_english:
                sc = 'english_maybe'
            elif old.get('jtype') == '事业编统考':
                sc = 'general'
            else:
                sc = 'teacher_unknown'
            # 更新 store 中的 subject_class
            store[url]['subject_class'] = sc
        jobs.append({
            'id': make_id(url), 'title': t, 'url': url,
            'origin_url': old.get('origin_url', ''),
            'source': old.get('source', ''), 'region': old.get('region', ''),
            'region_detail': old.get('region_detail', ''), 'jtype': old.get('jtype', ''),
            'english': old.get('english', bool(RE_ENGLISH.search(t))),
            'subject_class': sc,
            'bianzhi': old.get('bianzhi', 'check'),
            'age': old.get('age', ''), 'headcount': old.get('headcount'),
            'deadline': dl,
            'published': old.get('published') or old.get('first_seen', TODAY.isoformat()),
            'first_seen': old.get('first_seen', TODAY.isoformat()),
        })
    for url in stale:
        del store[url]

    # 3.5 整体清理：发布超过60天且没有有效截止日期的老公告不再展示
    kept = []
    for j in jobs:
        dl_ok = j['deadline'] and datetime.date.fromisoformat(j['deadline']) >= TODAY - datetime.timedelta(days=1)
        if (j['published'] or '') >= cutoff or dl_ok:
            kept.append(j)
        elif j['url'] in store:
            store[j['url']]['status'] = 'expired'
    jobs = kept

    # 流程公示：按发布时间倒序，最多保留 80 条
    process.sort(key=lambda p: p.get('published') or '', reverse=True)
    process = process[:80]

    # 4. 排序：报名未截止的按截止日期升序在最前，其余按发布时间倒序
    def to_ord(s):
        try:
            return datetime.date.fromisoformat(s).toordinal()
        except (ValueError, TypeError):
            return None

    def sort_key(j):
        d_ord = to_ord(j['deadline']) if j['deadline'] else None
        if d_ord and d_ord >= TODAY.toordinal() - 2:
            return (0, d_ord, 0)
        p_ord = to_ord(j['published']) or 0
        return (1, 0, -p_ord)

    jobs.sort(key=sort_key)

    # 5. 跨站点标题去重：同一公告被多个聚合站收录时 URL 不同会重复出现，
    #    规范化标题（去站尾/时间戳尾/空白、"公开招聘"归一为"招聘"、去结尾"公告"）后合并，
    #    并合并互为子串的长短标题（如"安宁中学招聘…"与"昆明安宁中学招聘…公告(数名)"）
    def norm_title(t):
        t = SITE_SUFFIX.sub('', t or '')
        t = re.sub(r'\s+', '', t)
        t = re.sub(r'20\d{2}-\d{1,2}-\d{1,2}.*$', '', t)   # 聚合站时间戳尾巴
        t = t.replace('公开招聘', '招聘')
        return re.sub(r'公告$', '', t)

    def title_richness(j):
        return (bool(j.get('deadline')), bool(j.get('origin_url')), len(j.get('title', '')))

    uniq = []
    seen = {}
    for j in jobs:
        nt = norm_title(j['title'])
        dup_idx = seen.get(nt)
        if dup_idx is None:
            for k, idx in seen.items():
                short, long_ = (k, nt) if len(k) <= len(nt) else (nt, k)
                if len(short) >= 12 and short in long_:
                    dup_idx = idx
                    break
        if dup_idx is None:
            seen[nt] = len(uniq)
            uniq.append(j)
        elif title_richness(j) > title_richness(uniq[dup_idx]):
            uniq[dup_idx] = j
    jobs = uniq

    today_str = TODAY.isoformat()
    for j in jobs:
        j['is_new'] = j.get('first_seen') == today_str

    data = {
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
        'generated_date': today_str,
        'sources': src_status,
        'total_candidates': len(candidates),
        'excluded': excluded,
        'jobs': jobs,
        'process': process,
    }

    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    with open(STORE_FILE, 'w', encoding='utf-8') as f:
        json.dump(store, f, ensure_ascii=False)

    # 5. 生成 index.html（把数据内嵌进模板，双击即可打开）
    if os.path.exists(TEMPLATE_FILE):
        with open(TEMPLATE_FILE, 'r', encoding='utf-8') as f:
            tpl = f.read()
        html_out = tpl.replace('"__JOBS_DATA__"', json.dumps(data, ensure_ascii=False))
        with open(INDEX_FILE, 'w', encoding='utf-8') as f:
            f.write(html_out)

    print('---')
    eng_clear = sum(1 for j in jobs if j['subject_class'] == 'english_clear')
    eng_maybe = sum(1 for j in jobs if j['subject_class'] == 'english_maybe')
    eng_str = '英语明确 %d' % eng_clear
    if eng_maybe:
        eng_str += ' / 可能含英语 %d' % eng_maybe
    print('收录 %d 条（曲靖 %d / %s），流程公示 %d 条，排除 %d 条（其中其他学科 %d），新抓详情 %d 页，解析岗位表 %d 个'
          % (len(jobs),
             sum(1 for j in jobs if j['region'] == '曲靖'),
             eng_str,
             len(process), excluded, subject_filtered, new_details, new_attachments))
    print('=== 完成，已生成 data.json / index.html ===')


if __name__ == '__main__':
    # 无窗口模式（pythonw/计划任务）下 stdout 为 None，重定向到空设备避免崩溃
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w', encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w', encoding='utf-8')
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    main()
