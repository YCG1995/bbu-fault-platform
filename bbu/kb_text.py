"""故障知识库文本层：归一化、分词、实体/PII、模板小节、SimHash。

只使用 Python 标准库。这里的函数必须可预测、可单测，不依赖数据库或模型。
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

# 只去掉极少量疑问/连接词，保留“不复现、未恢复”等否定语义。
STOPWORDS = frozenset({
    "的", "了", "和", "与", "及", "或", "是", "在", "有", "为", "对", "从",
    "the", "and", "for", "with", "this", "that", "from", "into", "than",
    "怎么", "如何", "什么", "为什么", "可以", "需要", "是否", "以及", "一个",
    "请问", "一下", "怎样", "哪些", "哪个", "多少", "多久", "帮忙",
})

ASCII_TERM_RE = re.compile(r"[a-z0-9][a-z0-9_.:/-]{1,}")
CHINESE_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
TAG_RE = re.compile(r"【([^】\n]{1,60})】")
BOARD_RE = re.compile(
    r"(?<![A-Za-z0-9])(VBP[A-Za-z0-9]{2,8}|VSW[A-Za-z0-9]{2,8}|"
    r"VBPc[A-Za-z0-9]{2,8}|VBPe[A-Za-z0-9]{2,8}|VBPd[A-Za-z0-9]{2,8}|"
    r"VSWd[A-Za-z0-9]{2,8}|VSWc[A-Za-z0-9]{2,8})(?![A-Za-z0-9])", re.I)
VERSION_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:NR-)?(?:V)?\d+(?:\.\d+){2,}(?:[-_A-Za-z0-9.]+)?", re.I)
HEX_VERSION_RE = re.compile(r"\b0x[0-9a-f]{2,8}\b", re.I)
SLOT_RE = re.compile(r"(?:slot\s*[\dA-Za-z]+|[\dA-Za-z]+号?槽位)", re.I)
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9])")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
EMP_NO_RE = re.compile(r"(?:工号|员工号|员工编号)\s*[:：]?\s*[\dA-Za-z]{5,16}")
NAME_NO_RE = re.compile(r"[\u4e00-\u9fff]{2,4}\d{7,8}")
URL_SAFE_RE = re.compile(r"[^0-9A-Za-z_.-]+")

DOMAIN_WORDS: dict[str, tuple[str, ...]] = {
    "电源": ("电源", "掉电", "过压", "欠压", "power", "供电"),
    "时钟": ("时钟", "1588", "失锁", "失步", "锁相", "clock", "1pps", "gnss"),
    "存储": ("存储", "ssd", "硬盘", "emmc", "usb", "u盘", "flash", "磁盘"),
    "版本升级": ("版本升级", "升级", "epld", "cpld", "bios", "0xfe", "版本"),
    "复位": ("复位", "重启", "watchdog", "看门狗", "wdg", "reboot", "reset"),
    "光": ("光模块", "sfp", "qsfp", "收光", "光口", "rx power", "optical"),
    "低速": ("低速", "ipmb", "i2c", "spi", "uart", "串口", "can"),
    "高速": ("高速", "pcie", "serdes", "以太", "eth", "链路"),
    "大容量": ("大容量", "小区建立", "cell setup", "小区退服", "话务"),
    "驻波": ("驻波", "vswr", "驻波比", "天线"),
    "过温": ("过温", "温度", "over temperature", "风扇", "fan", "风机"),
    "核心": ("核跑死", "core hang", "cpu", "单板初始化", "board init"),
}

SECTION_ALIASES: dict[str, str] = {
    "详细描述": "symptom", "测试输入": "symptom", "问题描述": "symptom",
    "测试环境": "environment", "环境": "environment", "组网": "environment",
    "预期结果": "expected", "期望结果": "expected",
    "实际结果": "actual", "实际现象": "actual",
    "现象是否可以复现": "repro", "是否可以复现": "repro", "复现概率": "repro",
    "发现日期": "found_date", "发生日期": "found_date",
    "发生地点": "location", "地点": "location", "站点": "location",
    "原因分析": "cause", "原因": "cause", "根因": "cause",
    "原因描述": "cause_detail", "根本原因": "cause_detail",
    "定性": "diagnosis", "是否其它变更引入": "diagnosis", "是否其他变更引入": "diagnosis",
    "处理方案": "solution", "解决方案": "solution", "修改建议": "solution",
    "处置方案": "solution", "恢复方案": "solution", "解决措施": "solution",
    "故障建议": "prevention", "变更影响分析": "prevention", "预防措施": "prevention",
    "定位结果": "diagnosis", "定位时间": "diagnosis", "发生器件": "diagnosis",
    "测试人员自定位信息": "diagnosis", "以往版本是否存在": "diagnosis",
    "版本信息": "version", "版本": "version",
    "日志": "log", "附件": "attachment",
}

TITLE_NOISE_RE = re.compile(
    r"^\s*(?:[\(（][^()（）]{0,80}[\)）]|\[[^\]]{0,80}\])+\s*", re.S)
TITLE_VERSION_RE = re.compile(r"^\s*[\(（]?\s*(?:NR-)?V\d+(?:\.\d+)+[-_A-Za-z0-9.]+[\)）]?\s*", re.I)
SPACE_RE = re.compile(r"\s+")
PUNCT_RE = re.compile(r"[\s\u3000:：,，;；。！？!?、|/]+")


@dataclass(frozen=True)
class Section:
    label: str
    kind: str
    text: str
    start: int = 0
    end: int = 0
    ordinal: int = 0

    def public(self) -> dict[str, Any]:
        return {"label": self.label, "kind": self.kind, "text": self.text,
                "ordinal": self.ordinal}


@dataclass
class CaseDraft:
    title: str
    title_clean: str
    symptom: str = ""
    environment: str = ""
    expected: str = ""
    actual: str = ""
    repro: str = ""
    found_date: str = ""
    location: str = ""
    cause: str = ""
    cause_detail: str = ""
    solution: str = ""
    prevention: str = ""
    root_cause_key: str = ""
    sections: list[Section] = field(default_factory=list)
    boards: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)
    quality: float = 0.0
    confidence: float = 0.9
    is_derived: bool = False
    ordinal: int = 1
    warnings: list[str] = field(default_factory=list)

    def fields(self) -> dict[str, str]:
        return {
            "title": self.title_clean or self.title,
            "symptom": self.symptom,
            "cause": self.cause_detail or self.cause,
            "solution": self.solution,
            "body": "\n".join(s.text for s in self.sections) or self.symptom,
        }

    def public(self) -> dict[str, Any]:
        return {
            "title": self.title, "title_clean": self.title_clean,
            "symptom": self.symptom, "environment": self.environment,
            "expected": self.expected, "actual": self.actual, "repro": self.repro,
            "found_date": self.found_date, "location": self.location,
            "cause": self.cause, "cause_detail": self.cause_detail,
            "solution": self.solution, "prevention": self.prevention,
            "root_cause_key": self.root_cause_key,
            "sections": [s.public() for s in self.sections],
            "boards": self.boards, "domains": self.domains, "versions": self.versions,
            "quality": self.quality, "confidence": self.confidence,
            "is_derived": self.is_derived, "ordinal": self.ordinal,
            "warnings": self.warnings,
        }


def normalize_text(value: Any) -> str:
    """NFKC、统一标点与空白，保留换行；用于展示和规则抽取。"""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("：", ":").replace("（", "(").replace("）", ")")
    text = text.replace("－", "-").replace("–", "-").replace("—", "-")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_key(value: Any) -> str:
    """检索用归一化：小写、去零宽、合并空白。"""
    return SPACE_RE.sub(" ", normalize_text(value).lower()).strip()


def section_kind(label: str) -> str:
    """把模板标签去序号、星号、空白后映射到规范 kind。"""
    key = normalize_text(label).strip()
    key = re.sub(r"^[\s*＊0-9一二三四五六七八九十.、\-]+", "", key)
    key = key.strip("*＊ \t")
    key = PUNCT_RE.sub("", key)
    return SECTION_ALIASES.get(key, "body")


def extract_sections(text: str) -> tuple[str, list[Section]]:
    """按【标签】切小节，返回前言和有序小节。标签缺失时整篇作为 body。"""
    raw = normalize_text(text)
    matches = list(TAG_RE.finditer(raw))
    if not matches:
        return raw, [Section(label="正文", kind="body", text=raw, start=0, end=len(raw))]
    preamble = raw[:matches[0].start()].strip()
    sections: list[Section] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        raw_label = match.group(1).strip()
        inline = ""
        if ":" in raw_label:
            raw_label, inline = raw_label.split(":", 1)
            raw_label = raw_label.strip()
            inline = inline.strip()
        body = "\n".join(part for part in (inline, raw[match.end():end].strip()) if part).strip()
        sections.append(Section(label=raw_label, kind=section_kind(raw_label), text=body,
                                start=match.start(), end=end, ordinal=i + 1))
    return preamble, sections


def _section_map(sections: Iterable[Section]) -> dict[str, str]:
    out: dict[str, str] = {}
    for sec in sections:
        if sec.text and sec.kind not in out:
            out[sec.kind] = sec.text
    return out


def clean_title(title: str) -> str:
    value = normalize_text(title).strip()
    value = TITLE_VERSION_RE.sub("", value).strip()
    value = TITLE_NOISE_RE.sub("", value).strip()
    value = re.sub(r"^\s*\[[^\]]+\]\s*", "", value).strip()
    return value or normalize_text(title).strip()


def detect_boards(text: str) -> list[str]:
    seen: list[str] = []
    for match in BOARD_RE.finditer(normalize_text(text)):
        value = match.group(1).upper()
        if value not in seen:
            seen.append(value)
    return seen


def detect_domains(text: str) -> list[str]:
    low = normalize_key(text)
    found: list[str] = []
    for domain, words in DOMAIN_WORDS.items():
        if any(word.lower() in low for word in words) and domain not in found:
            found.append(domain)
    # 标题前缀 [电源][时钟] 也视为领域。
    for match in re.finditer(r"\[([^\]]{1,12})\]", normalize_text(text)):
        value = match.group(1).strip()
        if value in DOMAIN_WORDS and value not in found:
            found.append(value)
    return found


def detect_versions(text: str) -> list[str]:
    raw = normalize_text(text)
    found: list[str] = []
    for match in VERSION_RE.finditer(raw):
        value = match.group(0).upper()
        # 排除纯 IP/日期类小段；至少含两个点并包含 V 或 NR。
        if ("V" in value or "NR-" in value) and value not in found:
            found.append(value)
    for match in HEX_VERSION_RE.finditer(raw):
        value = match.group(0).lower()
        if value not in found:
            found.append(value)
    return found


def detect_slots(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(0) for m in SLOT_RE.finditer(normalize_text(text))))


def mask_pii(text: str) -> tuple[str, bool]:
    """脱敏姓名+工号、电话、邮箱；返回文本和是否发生替换。"""
    raw = str(text or "")
    changed = False

    def repl(pattern: re.Pattern[str], value: str, label: str) -> str:
        nonlocal changed
        return pattern.sub(label, value)

    value = repl(EMAIL_RE, raw, "〔邮箱〕")
    value = repl(PHONE_RE, value, "〔电话〕")
    value = repl(EMP_NO_RE, value, "〔工号〕")
    value = repl(NAME_NO_RE, value, "〔人员〕")
    changed = value != raw
    return value, changed


def make_terms(text: Any) -> list[str]:
    """生成 ASCII 词项 + 中文连续片段 + 重叠二元组；供索引与查询共用。"""
    norm = normalize_key(text)
    terms: set[str] = set()
    for word in ASCII_TERM_RE.findall(norm):
        if word not in STOPWORDS and len(word) >= 2:
            terms.add(word)
    for run in CHINESE_RUN_RE.findall(norm):
        if run in STOPWORDS:
            continue
        if len(run) <= 24:
            terms.add(run)
        if len(run) == 1:
            terms.add(run)
        for i in range(len(run) - 1):
            bigram = run[i:i + 2]
            if bigram not in STOPWORDS:
                terms.add(bigram)
    return sorted(terms)


def _term_set(text: Any) -> set[str]:
    return set(make_terms(text))


QUERY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "看门狗": ("watchdog", "wdg", "狗复位", "看门狗复位"),
    "watchdog": ("看门狗", "wdg", "狗复位"),
    "复位": ("reset", "reboot", "反复重启", "不断重启", "起不来"),
    "reset": ("复位", "reboot", "反复重启"),
    "基带板": ("baseband board", "bbp", "基带"),
    "baseband": ("基带板", "基带", "bbp"),
    "光模块": ("sfp", "sfp+", "qsfp", "光口", "optical module"),
    "sfp": ("光模块", "光口", "optical module"),
    "接收光功率": ("收光", "rx power", "收光低", "接收光功率异常"),
    "收光": ("接收光功率", "rx power", "收光低"),
    "时钟": ("clock", "1588", "同步", "时钟失锁", "失步", "锁相环失锁"),
    "clock": ("时钟", "1588", "同步", "失锁"),
    "链路断": ("link down", "通讯链路断", "链路闪断", "单板通讯链路断"),
    "linkdown": ("链路断", "通讯链路断", "链路闪断"),
    "eplod": ("epl", "cpld", "逻辑版本", "逻辑器件"),
    "epl": ("epl", "cpld", "逻辑版本", "逻辑器件"),
    "ssd": ("硬盘", "固态盘", "系统盘", "ssd 读写失败"),
    "usbflash": ("u 盘", "usb 盘", "usb 无法识别"),
    "驻波": ("vswr", "驻波比", "驻波告警"),
    "vswr": ("驻波", "驻波比", "驻波告警"),
    "小区建立失败": ("cell setup fail", "小区建立不了", "小区退服"),
    "cellsetupfail": ("小区建立失败", "小区退服"),
    "核跑死": ("core hang", "cpu 跑死", "核 40"),
    "corehang": ("核跑死", "cpu 跑死"),
    "过温": ("over temperature", "温度过高", "过温关断"),
    "风扇": ("fan", "风机", "转速异常", "风扇故障"),
    "fan": ("风扇", "风机", "转速异常"),
    "电源": ("power", "电源故障", "电源异常", "掉电"),
    "power": ("电源", "电源故障", "电源异常"),
}


def expand_terms(text: str) -> list[str]:
    """只对概念词做最多 2 个同义词扩展，不扩板卡型号/版本号/告警码。"""
    low = normalize_key(text)
    expansions: list[str] = []
    for phrase, aliases in QUERY_SYNONYMS.items():
        if phrase in low:
            for alias in aliases[:2]:
                expansions.append(alias)
    return expansions


def simhash(text: Any, bits: int = 64) -> int:
    terms = make_terms(text)
    if not terms:
        return 0
    weights: dict[str, int] = {}
    for term in terms:
        weights[term] = weights.get(term, 0) + 1
    vector = [0] * bits
    for term, weight in weights.items():
        digest = int.from_bytes(
            hashlib.blake2b(term.encode("utf-8"), digest_size=bits // 8).digest(), "big")
        for i in range(bits):
            vector[i] += weight if (digest >> i) & 1 else -weight
    value = 0
    for i, item in enumerate(vector):
        if item >= 0:
            value |= 1 << i
    return value


def hamming(a: int, b: int) -> int:
    return int(a ^ b).bit_count()


def slug_id(prefix: str, payload: str, length: int = 8) -> str:
    digest = hashlib.sha256(normalize_key(payload).encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:length].upper()}"


def parse_found_date(text: str) -> str:
    """把常见日期写法规范成 YYYY-MM-DD；识别失败保留原文。"""
    raw = normalize_text(text)
    for pattern, fmt in (
        (r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)", "%Y-%m-%d"),
        (r"(?<!\d)(\d{4})/(\d{1,2})/(\d{1,2})(?!\d)", "%Y-%m-%d"),
        (r"(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})日", "%Y-%m-%d"),
    ):
        match = re.search(pattern, raw)
        if match:
            try:
                return datetime(*map(int, match.groups())).strftime(fmt)
            except ValueError:
                continue
    return raw[:40]


def section_text(sections: list[Section], kind: str) -> str:
    for sec in sections:
        if sec.kind == kind and sec.text:
            return sec.text
    return ""


def _quality(case: CaseDraft, source_confidence: float = 0.9) -> float:
    score = 0.0
    if case.symptom:
        score += 25
    if case.cause or case.cause_detail:
        score += 25
    if case.solution:
        score += 25
    if case.boards or case.domains:
        score += 10
    if case.versions or case.repro or case.found_date:
        score += 10
    score += max(0.0, min(1.0, source_confidence)) * 5
    return round(score, 1)


def build_cases(title: str, answer: str, *, source_confidence: float = 0.9,
                forced_title: str = "") -> tuple[list[CaseDraft], list[str]]:
    """把一条问答拆成 1..N 个案例；原因分析块是拆分边界。"""
    title_raw = normalize_text(forced_title or title)
    title_clean = clean_title(title_raw)
    preamble, sections = extract_sections(answer)
    warnings: list[str] = []
    base = _section_map(sections)
    cause_matches = [sec for sec in sections if sec.kind == "cause"]
    base_symptom = " ".join(x for x in (
        title_clean, base.get("symptom", ""), base.get("actual", ""), preamble[:300]
    ) if x).strip()
    boards = detect_boards(title_raw + "\n" + answer)
    domains = detect_domains(title_raw + "\n" + answer)
    versions = detect_versions(title_raw + "\n" + answer)
    blocks: list[list[Section]] = []
    if cause_matches:
        for index, cause_sec in enumerate(cause_matches):
            end = (cause_matches[index + 1].start if index + 1 < len(cause_matches)
                   else len(answer) + 1)
            block = [s for s in sections if s.start >= cause_sec.start and s.start < end]
            blocks.append(block)
    else:
        blocks = [sections]

    cases: list[CaseDraft] = []
    for index, block in enumerate(blocks):
        sec_map = _section_map(block)
        cause_text = sec_map.get("cause", "")
        cause_detail = sec_map.get("cause_detail", "")
        solution = sec_map.get("solution", "")
        if index > 0:
            # 派生案例若缺少方案，可从首个块继承全局方案；显式标注继承。
            solution = solution or base.get("solution", "")
        derived = index > 0
        case = CaseDraft(
            title=title_raw if not derived else f"{title_raw}（转交/派生）",
            title_clean=title_clean if not derived else f"{title_clean}（转交/派生）",
            symptom=base_symptom,
            environment=base.get("environment", ""),
            expected=base.get("expected", ""),
            actual=base.get("actual", ""),
            repro=base.get("repro", ""),
            found_date=parse_found_date(base.get("found_date", "")),
            location=base.get("location", ""),
            cause=cause_text or ("；".join(x for x in (sec_map.get("diagnosis", ""),
                                                      cause_detail[:160]) if x)),
            cause_detail=cause_detail or cause_text,
            solution=solution,
            prevention=sec_map.get("prevention", "") or base.get("prevention", ""),
            root_cause_key=slug_id("RC", cause_detail or cause_text or title_clean),
            sections=block,
            boards=boards, domains=domains, versions=versions,
            confidence=source_confidence, is_derived=derived, ordinal=index + 1,
        )
        if not case.cause and not case.cause_detail:
            case.warnings.append("缺少原因分析，进入待补充队列")
        if not case.solution:
            case.warnings.append("缺少处理方案")
        if not case.symptom:
            case.warnings.append("缺少现象描述")
        case.quality = _quality(case, source_confidence)
        cases.append(case)
    if not cases:
        cases.append(CaseDraft(
            title=title_raw, title_clean=title_clean, symptom=base_symptom,
            boards=boards, domains=domains, versions=versions,
            confidence=source_confidence, sections=sections,
            warnings=["正文为空"]))
        cases[0].quality = _quality(cases[0], source_confidence)
    if not boards:
        warnings.append("未识别到板卡型号")
    if not domains:
        warnings.append("未识别到领域标签")
    return cases, warnings


def excerpt(text: str, limit: int = 420) -> str:
    value = SPACE_RE.sub(" ", normalize_text(text))
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


def token_weights(fields: dict[str, str]) -> dict[str, int]:
    """返回字段 -> 词项计数的轻量统计，供评测和调试使用。"""
    return {name: len(_term_set(value)) for name, value in fields.items()}


def freshness_score(iso_text: str, now: datetime | None = None) -> float:
    """近两年轻微加分；日期无法解析返回 0。"""
    if not iso_text:
        return 0.0
    try:
        value = datetime.fromisoformat(str(iso_text).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 0.0
    now = now or datetime.now(timezone.utc)
    years = max(0.0, (now - value).days / 365.25)
    return max(0.0, min(1.0, 1.0 - years / 4.0))


def cosine_text_similarity(a: str, b: str) -> float:
    """无向量依赖的轻量相似度，仅用于近重复解释。"""
    ta, tb = _term_set(a), _term_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / math.sqrt(len(ta) * len(tb))
