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
MAX_NEW_DETAILS_PER_RUN = 40  # 每次运行最多抓取的详情页数量（礼貌抓取）

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


def abs_url(href, base):
    """相对链接 -> 绝对链接"""
    if href.startswith(('http://', 'https://')):
        return href
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
RE_QUJING = re.compile(r'曲靖|麒麟|沾益|马龙|陆良|师宗|罗平|富源|会泽|宣威')
RE_KUNMING = re.compile(r'昆明|云南师范大学|安宁|嵩明|宜良|石林|寻甸|禄劝|东川|富民|晋宁')
RE_ZHAOTONG = re.compile(r'昭通|昭阳|鲁甸|巧家|彝良|威信|镇雄|大关|永善|绥江|水富')
RE_GUIZHOU_NEAR = re.compile(r'兴义|黔西南|六盘水|盘州')
RE_YUNNAN = re.compile(r'云南')
RE_GUIZHOU_FAR = re.compile(r'贵州|贵阳|遵义|毕节|安顺|黔东南|黔南|铜仁')
RE_OTHER_PROVINCE = re.compile(
    r'广东|广西|四川|重庆|湖南|湖北|河南|河北|山东|山西|陕西|甘肃|江苏|浙江|安徽|福建|江西|'
    r'海南|贵州|内蒙古|新疆|西藏|青海|宁夏|黑龙江|吉林|辽宁|天津|上海|北京')

RE_OPP = re.compile(r'(招聘|引进|特岗|选调|招募|考聘)')          # 机会类标题
RE_RESULT = re.compile(                                          # 结果/流程类标题（非机会）
    r'(成绩|资格复审|体检|考察|拟聘|拟录|拟进|名单|公示|更正|方案|通告|温馨提示|'
    r'工作安排|大纲|成绩查询|结果|递补|面试通知|准考证|答复|回复|处理情况)')
RE_INFO = re.compile(                                            # 资讯/问答类文章（非公告）
    r'(考什么|什么时候|怎么考|报名入口|查询时间|最新招聘信息|入面分数|分数线|多少分|'
    r'考试科目|科目有哪些|待遇|怎么样|难吗|备考|真题|答案|职位表下载|汇总$|时间安排|'
    r'报考指南|常见问题|招聘信息$)')
RE_TEACHER = re.compile(
    r'教师|教育|学校|学院|幼儿园|师范|教体|教学|中学|小学|职校|技工学校')
RE_UNIT = re.compile(r'事业单位')  # 事业单位公开招聘（未必带"教师"字样，如统考公告）
RE_NEG = re.compile(
    r'编制外|编外|合同制|劳动合同制|劳务派遣|人事代理|临聘|代课|顶岗|公益性岗位|'
    r'非全日制|辅助人员|政府购买|购买服务|非在编|第三方|民办')
RE_POS_STRONG = re.compile(
    r'事业编制|纳入编制|编制内|使用编制|特岗计划|特岗教师|公费师范|公费师范生|'
    r'公开引进|公开招聘|人才引进|事业单位公开招聘')
RE_ENGLISH = re.compile(r'英语')
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
                  'https://hrss.yn.gov.cn/NewsLsit.aspx?ClassID=602'],
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
    if RE_ZHAOTONG.search(title):
        return '昭通', ''
    if RE_GUIZHOU_NEAR.search(title):
        return '贵州邻近', ''
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
    info['english'] = bool(RE_ENGLISH.search(text))
    info['neg_hit'] = bool(RE_NEG.search(text))
    return info


def make_id(url):
    return hashlib.md5(url.encode('utf-8')).hexdigest()[:12]


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
    excluded = 0
    new_details = 0
    for url, cand in candidates.items():
        title = cand['title']

        # 地区判定
        if cand['force_region']:
            region, region_detail = cand['force_region'], ''
            rd = classify_region(title)
            if rd[1]:
                region_detail = rd[1]
        else:
            region, region_detail = classify_region(title)
        if not region:
            excluded += 1
            continue

        # 教师/事业单位相关性（防止企业招聘、医疗卫生等混入）
        if not (RE_TEACHER.search(title) or RE_UNIT.search(title) or '特岗' in title):
            excluded += 1
            continue

        jtype = classify_type(title)

        # 存量里是否已有该 URL 的分析结果
        item_id = make_id(url)
        old = store.get(url)

        need_detail = old is None
        if old and not old.get('analyzed'):
            need_detail = True

        body_text = old.get('body_text', '') if old else ''
        if need_detail and new_details < MAX_NEW_DETAILS_PER_RUN:
            try:
                html = fetch(url, timeout=20)
                body_text = extract_text(html)[:20000]
                new_details += 1
                time.sleep(0.4)
            except Exception as e:
                print('  [detail fail] %s: %s' % (title[:30], e))
                body_text = ''

        body_info = analyze_body(body_text)

        # 编制判断：先负面后正面
        neg = bool(RE_NEG.search(title)) or body_info['neg_hit']
        pos = bool(RE_POS_STRONG.search(title) or RE_POS_STRONG.search(body_text))
        if neg:
            store[url] = {
                'title': title, 'status': 'excluded_neg', 'source': cand['source'],
                'analyzed': True, 'body_text': body_text[:5000],
                'first_seen': (old or {}).get('first_seen', TODAY.isoformat()),
            }
            excluded += 1
            continue
        if not old:
            store[url] = {}
        store[url].update({
            'title': title, 'status': 'included', 'source': cand['source'],
            'region': region, 'region_detail': region_detail, 'jtype': jtype,
            'analyzed': bool(body_text),
        })

        bianzhi = 'yes' if pos else 'check'
        english = bool(RE_ENGLISH.search(title)) or body_info['english']
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
            'source': cand['source'],
            'region': region,
            'region_detail': region_detail,
            'jtype': jtype,
            'english': english,
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
        if body_text:
            store[url]['body_text'] = body_text[:8000]

    # 3. 合并存量中之前收录、本次列表里没出现的机会（可能还在报名期内），60天前的一律清理
    seen_urls = {j['url'] for j in jobs}
    cutoff = (TODAY - datetime.timedelta(days=60)).isoformat()
    stale = []
    for url, old in store.items():
        if old.get('status') != 'included' or url in seen_urls:
            continue
        t = old.get('title', '')
        if RE_RESULT.search(t) or RE_INFO.search(t):
            stale.append(url)
            continue
        if (old.get('published') or old.get('first_seen', '')) < cutoff:
            stale.append(url)
            continue
        dl = old.get('deadline')
        if dl and datetime.date.fromisoformat(dl) <= TODAY - datetime.timedelta(days=1):
            stale.append(url)
            continue
        jobs.append({
            'id': make_id(url), 'title': t, 'url': url,
            'source': old.get('source', ''), 'region': old.get('region', ''),
            'region_detail': old.get('region_detail', ''), 'jtype': old.get('jtype', ''),
            'english': old.get('english', bool(RE_ENGLISH.search(t))),
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
    print('收录 %d 条（曲靖 %d / 英语岗 %d），排除 %d 条，新抓详情 %d 页'
          % (len(jobs),
             sum(1 for j in jobs if j['region'] == '曲靖'),
             sum(1 for j in jobs if j['english']),
             excluded, new_details))
    print('=== 完成，已生成 data.json / index.html ===')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    main()
