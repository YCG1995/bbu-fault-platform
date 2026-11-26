"""平台文档知识库：Markdown 分块、BM25F 检索、资源读取。

悬浮助手与 MCP 资源共用同一份索引；不引入向量库、numpy、onnxruntime 或外部依赖。
索引在第一次检索时懒加载并常驻进程内存，文档读取与资源 URI 契约保持不变。
"""
from __future__ import annotations

import math
import re
import threading
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_CHUNK = 1500
MAX_DOC_BYTES = 160_000
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
ASCII_TERM_RE = re.compile(r"[a-zA-Z0-9_.:/-]{2,}")
CHINESE_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")

DOC_FILES: tuple[tuple[str, str, str], ...] = (
    ("readme", "README.md", "平台总览、快速开始、目录结构与测试"),
    ("user-guide", "docs/使用手册.md", "日常使用：诊断、技能、工坊、账号与通知"),
    ("deploy-guide", "docs/部署手册.md", "Linux/Windows 部署、服务、备份与排障"),
    ("oneclick-deploy", "docs/一键部署说明.md", "离线一键包安装与 Ubuntu 16 兼容"),
    ("upgrade-rollback", "docs/升级与回滚.md", "网页升级、离线包、回滚与升级助手"),
    ("architecture", "docs/架构设计.md", "后端模块、任务编排、数据模型与安全边界"),
    ("windows", "docs/Windows运行说明.md", "Windows 启动、服务与升级入口"),
    ("upgrade-sudo", "docs/升级助手sudo故障修复.md", "sudo setuid/nosuid 与升级助手修复"),
    ("workshop", "docs/技能工坊AI生成优化建议.md", "技能工坊生成、评审与配方"),
    ("release-1.24.5", "docs/版本说明-1.24.5.md", "当前版本 MCP 客户端配置：说明/代码分区、复制 JSON 与移动端布局"),
    ("release-1.24.4", "docs/版本说明-1.24.4.md", "1.24.4 迁移页布局：覆盖恢复分区、自定义迁移包选择与记录空态"),
    ("release-1.24.3", "docs/版本说明-1.24.3.md", "1.24.3 任务空间布局：右侧工具栏、平铺概览、空态与移动卡片"),
    ("release-1.24.2", "docs/版本说明-1.24.2.md", "1.24.2 技能工坊空态布局：全宽起始卡与五步流程"),
    ("release-1.24.1", "docs/版本说明-1.24.1.md", "1.24.1 用户每日用量图表：按日堆叠用户与指标切换"),
    ("release-1.24.0", "docs/版本说明-1.24.0.md", "1.24.0 用户每日用量：每用户每日调用与 Token"),
    ("release-1.23.0", "docs/版本说明-1.23.0.md", "1.23.0 用量统计：模型每日、任务 Token 与技能消耗"),
    ("release-1.22.0", "docs/版本说明-1.22.0.md", "1.22.0 指定技能、全量证据、执行轨迹与归档加固"),
    ("release-1.20.3", "docs/版本说明-1.20.3.md", "1.20.3 知识库检索 P0/P1、BM25F 与评测"),
    ("release-1.20.2", "docs/版本说明-1.20.2.md", "上一版本服务单元 NoNewPrivileges 修复与升级助手"),
    ("diagnostic-data", "docs/诊断推理数据说明.md", "推理数据、证据与案例字段"),
)

DOC_PRIORITY = {
    "user-guide": 3.5, "deploy-guide": 2.0, "oneclick-deploy": 2.0,
    "upgrade-rollback": 2.5, "upgrade-sudo": 1.5, "architecture": 1.5,
    "windows": 1.5, "workshop": 1.5, "diagnostic-data": 1.0,
    "readme": 0.5, "release-1.24.5": 0.3, "release-1.24.4": 0.3, "release-1.24.3": 0.3, "release-1.24.2": 0.3, "release-1.24.1": 0.3, "release-1.24.0": 0.2,
    "release-1.20.3": 0.2, "release-1.20.2": 0.2,
}

STOPWORDS = frozenset({
    "the", "and", "for", "with", "this", "that", "from", "into", "than",
    "怎么", "如何", "什么", "为什么", "可以", "需要", "是否", "以及", "一个",
    "是什么", "是什", "这是", "怎样", "哪些", "哪个", "多少", "多久",
})

# BM25F 字段权重：章节标题优先于正文；文档标题是弱字段。
FIELD_WEIGHTS = {"heading": 3.0, "title": 2.0, "body": 1.0}
BM25_K1 = 1.2
BM25_B = 0.75


@dataclass(frozen=True)
class DocChunk:
    slug: str
    doc_title: str
    heading: str
    text: str
    order: int


@dataclass(frozen=True)
class DocEntry:
    slug: str
    title: str
    description: str
    path: str


@dataclass(frozen=True)
class _ChunkStats:
    heading: Counter[str]
    title: Counter[str]
    body: Counter[str]
    weighted_len: float


@dataclass(frozen=True)
class _IntentRule:
    name: str
    pattern: re.Pattern[str]
    slug: str
    boost: float
    heading_terms: tuple[str, ...] = ()


# 常见问法/同义词；只做词项扩展和文档/章节路由，不引入新依赖。
_QUERY_ALIASES: dict[str, tuple[str, ...]] = {
    "新建诊断": ("发起诊断", "创建诊断", "诊断任务", "日志包"),
    "发起诊断": ("新建诊断", "创建诊断", "诊断任务"),
    "创建诊断": ("新建诊断", "发起诊断", "诊断任务"),
    "诊断任务": ("新建诊断", "发起诊断"),
    "上传日志": ("日志包", "诊断任务", "附件"),
    "改密码": ("修改密码", "重置密码", "账号密码"),
    "忘记密码": ("重置密码", "修改密码", "账号密码"),
    "登录不上": ("登录失败", "登录", "权限", "密码"),
    "账号锁定": ("登录失败", "锁定", "账号"),
    "升级助手": ("sudo", "root 助手", "systemctl", "setuid"),
    "网页升级": ("离线升级", "升级包", "上传升级"),
    "自动升级": ("网页上传升级", "升级", "回滚"),
    "升级失败": ("升级错误", "回滚", "版本", "未知错误"),
    "回滚": ("恢复", "rollback", "备份"),
    "恢复": ("回滚", "备份", "迁移"),
    "一键包": ("一键部署", "初始包", "ubuntu 16", "离线安装"),
    "一键部署": ("初始包", "ubuntu 16", "安装"),
    "初始包": ("一键部署", "一键包", "ubuntu 16"),
    "离线安装": ("一键部署", "离线包", "部署"),
    "工作目录": ("迁移", "workspace", "数据目录"),
    "迁移": ("工作目录", "导入导出", "备份"),
    "备份": ("历史备份", "恢复", "数据目录"),
    "模型接入": ("maas", "api key", "模型配置", "模型供应商"),
    "模型配置": ("maas", "api key", "模型接入"),
    "技能工坊": ("生成技能", "配方", "工坊", "技能"),
    "生成技能": ("技能工坊", "配方", "生成"),
    "mcp": ("mcp", "令牌", "token", "外部接入"),
    "令牌": ("token", "mcp", "api key"),
    "悬浮助手": ("平台助手", "助手", "mcp"),
    "诊断数据": ("推理数据", "证据", "案例字段"),
    "推理数据": ("诊断数据", "证据", "案例字段"),
    "windows": ("windows", "start_platform", "bat"),
    "服务": ("systemd", "服务管理", "重启"),
    "重启": ("服务管理", "systemctl", "重启平台"),
}


_QUERY_RULES: tuple[_IntentRule, ...] = (
    _IntentRule(
        "新建诊断",
        re.compile(r"新建诊断|发起诊断|创建诊断|诊断任务|上传日志|日志包"),
        "user-guide", 10.0, ("发起一次诊断", "日志包建议"),
    ),
    _IntentRule(
        "升级助手 sudo",
        re.compile(r"有效用户\s*id|no\s*new\s*privileges|nnp|nosuid|setuid|"
                   r"systemctl.*(?:没有|找不到|不存在)|sudo.*(?:失败|不是|权限)|"
                   r"root\s*升级助手"),
        "upgrade-sudo", 12.0, ("sudo",),
    ),
    _IntentRule(
        "NoNewPrivileges 确认",
        re.compile(r"no\s*new\s*privileges.*(?:确认|检查|是不是)|"
                   r"(?:确认|检查|是不是).*no\s*new\s*privileges"),
        "upgrade-sudo", 14.0, ("2. 先确认",),
    ),
    _IntentRule(
        "NoNewPrivileges 修复",
        re.compile(r"no\s*new\s*privileges.*(?:修复|解决)|"
                   r"(?:修复|解决).*no\s*new\s*privileges"),
        "upgrade-sudo", 14.0, ("4. root 一键修复",),
    ),
    _IntentRule(
        "1.24.5 MCP 客户端配置布局",
        re.compile(r"1\.24\.5|mcp.*(?:客户端|配置|布局)|客户端配置.*(?:布局|优化)"),
        "release-1.24.5", 12.0,
    ),
    _IntentRule(
        "1.24.4 迁移页布局",
        re.compile(r"1\.24\.4|迁移.*(?:布局|页面|覆盖恢复)|覆盖恢复.*(?:布局|页面)"),
        "release-1.24.4", 12.0,
    ),
    _IntentRule(
        "1.24.3 任务空间布局",
        re.compile(r"1\.24\.3|任务空间.*(?:布局|优化)|空间.*(?:统计|释放).*(?:布局|页面)"),
        "release-1.24.3", 12.0,
    ),
    _IntentRule(
        "1.24.2 工坊空态布局",
        re.compile(r"1\.24\.2|工坊.*(?:空态|布局)|技能工坊.*(?:布局|首屏)"),
        "release-1.24.2", 12.0,
    ),
    _IntentRule(
        "1.24.1 用户每日图表",
        re.compile(r"1\.24\.1|用户.*图表|每日.*图表|用户每日用量"),
        "release-1.24.1", 12.0,
    ),
    _IntentRule(
        "1.24.0 用户每日用量",
        re.compile(r"1\.24\.0|用户每日|每个用户.*(?:每天|每日)|用户.*用量"),
        "release-1.24.0", 12.0,
    ),
    _IntentRule(
        "1.23.0 用量统计",
        re.compile(r"1\.23\.0|用量统计|每日.*模型|任务.*token|技能.*消耗"),
        "release-1.23.0", 12.0,
    ),
    _IntentRule(
        "1.22.0 新特性",
        re.compile(r"1\.22\.0|指定技能|全量证据|执行轨迹|create_skill_diagnosis"),
        "release-1.22.0", 12.0,
    ),
    _IntentRule(
        "1.20.3 检索优化",
        re.compile(r"1\.20\.3|检索优化|知识库检索|bm25"),
        "release-1.20.3", 10.0,
    ),
    _IntentRule(
        "升级后错误",
        re.compile(r"(?:升级|更新).*(?:未知错误|连接中断|版本.*(?:变|新)|失败)|"
                   r"未知错误.*(?:升级|版本)"),
        "release-1.20.2", 12.0, ("版本说明", "根因", "验证"),
    ),
    _IntentRule(
        "1.20.2 修复内容",
        re.compile(r"1\.20\.2.*(?:修复|变更)|修复了什么"),
        "release-1.20.2", 14.0, ("三、修复内容",),
    ),
    _IntentRule(
        "1.20.2 验证",
        re.compile(r"1\.20\.2.*验证|怎么验证"),
        "release-1.20.2", 14.0, ("四、验证",),
    ),
    _IntentRule(
        "1.20.2 限制",
        re.compile(r"1\.20\.2.*(?:限制|边界)|已知限制"),
        "release-1.20.2", 14.0, ("六、已知限制",),
    ),
    _IntentRule(
        "升级",
        re.compile(r"升级|更新包|离线包|标准升级"),
        "upgrade-rollback", 6.0, ("平台升级与回滚", "网页上传升级", "标准升级", "1. 标准升级命令"),
    ),
    _IntentRule(
        "标准升级命令",
        re.compile(r"标准升级|升级命令"),
        "upgrade-rollback", 12.0, ("1. 标准升级命令",),
    ),
    _IntentRule(
        "升级后确认",
        re.compile(r"升级后.*确认|升级成功.*确认|确认.*升级成功"),
        "upgrade-rollback", 12.0, ("3. 升级后确认",),
    ),
    _IntentRule(
        "升级失败处理",
        re.compile(r"升级失败.*(?:处理|怎么办|怎么)|(?:处理|怎么办).*升级失败"),
        "upgrade-rollback", 12.0, ("6. 升级失败怎么处理",),
    ),
    _IntentRule(
        "回滚",
        re.compile(r"回滚|rollback"),
        "upgrade-rollback", 10.0, ("4. 回滚", "完整回滚", "只回滚代码"),
    ),
    _IntentRule(
        "手工部署",
        re.compile(r"手工部署|没有\s*systemd|无\s*systemd"),
        "upgrade-rollback", 12.0, ("5. 手工进程部署",),
    ),
    _IntentRule(
        "升级迁移恢复",
        re.compile(r"工作目录切换|迁移恢复|切换后.*恢复"),
        "upgrade-rollback", 12.0, ("6.4 工作目录切换与迁移恢复",),
    ),
    _IntentRule(
        "网页升级流程",
        re.compile(r"网页.*升级.*(?:怎么|如何|操作|流程)|自动升级|上传升级"),
        "upgrade-rollback", 12.0, ("6.2 网页流程",),
    ),
    _IntentRule(
        "一键部署",
        re.compile(r"一键包|一键部署|初始包|ubuntu\s*16|离线安装"),
        "oneclick-deploy", 6.0,
    ),
    _IntentRule(
        "一键包装安装",
        re.compile(r"(?:一键包|初始包|ubuntu\s*16).*(?:安装|怎么装)|"
                   r"(?:安装|怎么装).*(?:一键包|初始包|ubuntu\s*16)"),
        "oneclick-deploy", 10.0, ("2. 安装",),
    ),
    _IntentRule(
        "一键包区别",
        re.compile(r"一键包.*(?:区别|是什么|是什么包)|更新包.*区别"),
        "oneclick-deploy", 14.0, ("1. 这是什么",),
    ),
    _IntentRule(
        "一键包后续更新",
        re.compile(r"一键包.*(?:安装后|后续).*更新|一键包.*怎么更新"),
        "oneclick-deploy", 14.0, ("3. 后续更新",),
    ),
    _IntentRule(
        "一键包构建",
        re.compile(r"一键包.*构建|构建.*一键包"),
        "oneclick-deploy", 14.0, ("4. 构建方法",),
    ),
    _IntentRule(
        "一键包验证",
        re.compile(r"一键包.*验证|验证.*一键包"),
        "oneclick-deploy", 12.0, ("5. 验证结果",),
    ),
    _IntentRule(
        "网页升级故障",
        re.compile(r"网页.*升级.*(?:不可用|失败|报错|错误)|网页升级不可用"),
        "oneclick-deploy", 32.0, ("6. 常见问题",),
    ),
    _IntentRule(
        "升级后版本与错误",
        re.compile(r"版本.*(?:变|新)|升级.*完成.*未知错误|未知错误.*版本"),
        "oneclick-deploy", 16.0, ("6. 常见问题",),
    ),
    _IntentRule(
        "安装部署",
        re.compile(r"安装|部署|systemd|服务管理"),
        "deploy-guide", 4.0, ("内网服务器安装", "服务管理", "环境要求"),
    ),
    _IntentRule(
        "账号权限",
        re.compile(r"账号|用户|密码|权限|登录|审计"),
        "user-guide", 6.0, ("登录与权限", "用户与审计"),
    ),
    _IntentRule(
        "模型接入",
        re.compile(r"模型接入|maas|api\s*key|模型配置|token|令牌"),
        "user-guide", 6.0, ("模型接入", "MCP Token"),
    ),
    _IntentRule(
        "技能工坊使用",
        re.compile(r"技能工坊.*(?:怎么|如何|使用|操作|生成|新建)|生成技能"),
        "user-guide", 8.0, ("6. 技能工坊",),
    ),
    _IntentRule(
        "技能工坊评审",
        re.compile(r"技能工坊.*(?:评审|问题|建议|优化|模拟|结论)|配方|案例.*技能"),
        "workshop", 8.0, ("技能工坊", "配方", "评审"),
    ),
    _IntentRule(
        "工作目录",
        re.compile(r"备份|迁移|工作目录|恢复|workspace"),
        "user-guide", 5.0, ("工作目录与迁移", "历史备份", "任务空间"),
    ),
    _IntentRule(
        "Windows",
        re.compile(r"windows|start_platform|\.bat"),
        "windows", 6.0, (),
    ),
    _IntentRule(
        "架构",
        re.compile(r"架构|数据库|schema|线程|模块"),
        "architecture", 4.0, ("架构设计", "目录与部署"),
    ),
    _IntentRule(
        "MCP 助手",
        re.compile(r"mcp|悬浮助手|外部\s*im|飞书|豆包"),
        "user-guide", 4.0, ("平台助手与 MCP",),
    ),
    _IntentRule(
        "系统状态",
        re.compile(r"系统状态|系统信息|磁盘|运行参数"),
        "user-guide", 12.0, ("9. 系统信息",),
    ),
    _IntentRule(
        "进程线程模型",
        re.compile(r"进程|线程|线程模型"),
        "architecture", 12.0, ("2. 进程与线程模型",),
    ),
    _IntentRule(
        "数据与安全",
        re.compile(r"数据与安全|安全边界|数据模型|数据库.*表"),
        "architecture", 12.0, ("7. 数据与安全",),
    ),
    _IntentRule(
        "诊断推理证据",
        re.compile(r"诊断.*证据|证据规则|推理数据|案例字段|证据字段"),
        "diagnostic-data", 12.0, ("3. 推理数据结构",),
    ),
    _IntentRule(
        "诊断数据",
        re.compile(r"诊断推理数据|推理数据|案例字段|证据字段"),
        "diagnostic-data", 5.0, ("诊断推理数据说明", "推理数据结构"),
    ),
)


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _normalize(text: Any) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).lower().strip()


def _terms(text: Any, *, query: bool = False) -> list[str]:
    """提取 ASCII 词项、中文连续片段和相邻 bigram。"""
    norm = _normalize(text)
    terms: set[str] = set()
    for word in ASCII_TERM_RE.findall(norm):
        if word not in STOPWORDS:
            terms.add(word)
    for run in CHINESE_RUN_RE.findall(norm):
        if run in STOPWORDS:
            continue
        # 文档索引不保留过长的中文连续片段，避免低区分度长词膨胀。
        if len(run) <= (24 if query else 12):
            terms.add(run)
        for i in range(len(run) - 1):
            bigram = run[i:i + 2]
            if bigram not in STOPWORDS:
                terms.add(bigram)
    return list(terms)


def _query_terms(query: str) -> list[str]:
    q = _normalize(query)
    terms = set(_terms(q, query=True))
    for phrase, expansions in _QUERY_ALIASES.items():
        if phrase in q:
            for expansion in expansions:
                terms.update(_terms(expansion, query=True))
    return sorted(terms, key=lambda item: (-len(item), item))[:32]


_EXACT_IDENT_RE = re.compile(
    r"(?:[A-Za-z_][A-Za-z0-9_.:/-]{3,}|/[A-Za-z0-9_./{}:-]{2,}|[0-9]+(?:\.[0-9]+)+)")


def _exact_identifiers(query: str) -> list[str]:
    """挑出任务号、错误码、路径、命令和版本号等精确标识符。"""
    found: list[str] = []
    for token in _EXACT_IDENT_RE.findall(str(query or "")):
        low = token.lower()
        if any(ch in token for ch in "/_.:"):
            found.append(token)
        elif low in {"systemctl", "sudo", "nonewprivileges", "sqlite", "glibc"}:
            found.append(token)
        elif re.fullmatch(r"u[a-z0-9_-]{6,40}", low):
            found.append(token)
    return list(dict.fromkeys(found))


def _markdown_lines(text: str):
    """逐行给出“是否允许识别标题”，跳过 fenced code block 内的 # 行。"""
    in_fence = False
    fence = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence = marker
            elif marker == fence:
                in_fence = False
                fence = ""
            yield line, False
            continue
        yield line, not in_fence


def _first_heading(text: str, fallback: str) -> str:
    for line, allow_heading in _markdown_lines(text):
        if not allow_heading:
            continue
        match = HEADING_RE.match(line)
        if match:
            return match.group(2).strip()
    return fallback


def _split_long(text: str, limit: int = MAX_CHUNK) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", limit // 2, limit)
        if cut <= 0:
            cut = rest.rfind("。", limit // 2, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return [p for p in parts if p]


def _field_stats(chunk: DocChunk) -> _ChunkStats:
    heading = Counter(_terms(chunk.heading))
    title = Counter(_terms(chunk.doc_title))
    body = Counter(_terms(chunk.text))
    weighted_len = (
        FIELD_WEIGHTS["heading"] * sum(heading.values())
        + FIELD_WEIGHTS["title"] * sum(title.values())
        + FIELD_WEIGHTS["body"] * sum(body.values())
    )
    return _ChunkStats(heading=heading, title=title, body=body, weighted_len=weighted_len)


def _excerpt(text: str, query: str, limit: int = 420) -> str:
    # 保留代码内容，只去掉围栏和 Markdown 装饰，便于展示命令/路径。
    cleaned = re.sub(r"```[^\n]*\n?", " ", text)
    cleaned = cleaned.replace("```", " ").replace("`", " ")
    cleaned = re.sub(r"[*_>`#|]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= limit:
        return cleaned

    terms = _query_terms(query)
    if not terms:
        return cleaned[:limit].rstrip() + "…"

    best_start = 0
    best_score = -1.0
    step = max(20, limit // 8)
    for start in range(0, max(1, len(cleaned) - limit + 1), step):
        window = cleaned[start:start + limit].lower()
        score = 0.0
        for term in terms:
            count = min(3, window.count(term))
            if count:
                score += count * (1.6 if len(term) >= 4 else 1.0)
        if score > best_score:
            best_score = score
            best_start = start
    if best_score <= 0:
        best_start = 0
    prefix = "…" if best_start else ""
    suffix = "…" if best_start + limit < len(cleaned) else ""
    return prefix + cleaned[best_start:best_start + limit].strip() + suffix


class KnowledgeBase:
    """按 Markdown 标题分块；搜索只在本机内存完成。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()
        self._chunks: list[DocChunk] | None = None
        self._docs: list[DocEntry] | None = None
        self._raw: dict[str, tuple[DocEntry, str]] = {}
        self._stats: list[_ChunkStats] | None = None
        self._df: Counter[str] = Counter()
        self._avg_len = 1.0

    def _load(self) -> None:
        if self._chunks is not None and self._docs is not None:
            return
        with self._lock:
            if self._chunks is not None and self._docs is not None:
                return
            chunks: list[DocChunk] = []
            docs: list[DocEntry] = []
            raw: dict[str, tuple[DocEntry, str]] = {}
            for slug, relative, description in DOC_FILES:
                path = (self.root / relative).resolve()
                try:
                    path.relative_to(self.root.resolve())
                except ValueError:
                    continue
                if not path.is_file() or path.stat().st_size > MAX_DOC_BYTES:
                    continue
                try:
                    text = _clean(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                title = _first_heading(text, path.stem)
                entry = DocEntry(slug=slug, title=title, description=description,
                                 path=relative)
                docs.append(entry)
                raw[slug] = (entry, text)
                heading_stack: list[str] = []
                current_lines: list[str] = []
                current_heading = title
                order = 0

                def flush() -> None:
                    nonlocal order
                    body = "\n".join(current_lines).strip()
                    if not body:
                        return
                    for part in _split_long(body):
                        chunks.append(DocChunk(slug=slug, doc_title=title,
                                               heading=current_heading, text=part,
                                               order=order))
                        order += 1

                # 代码围栏内的 # 行不是章节标题，避免伪章节和错误 breadcrumb。
                for line, allow_heading in _markdown_lines(text):
                    match = HEADING_RE.match(line) if allow_heading else None
                    if match:
                        flush()
                        current_lines = []
                        level = len(match.group(1))
                        heading_stack = heading_stack[:level - 1] + [match.group(2).strip()]
                        current_heading = " / ".join(h for h in heading_stack if h)
                    else:
                        current_lines.append(line)
                flush()
            self._chunks = chunks
            self._docs = docs
            self._raw = raw
            self._stats = [_field_stats(chunk) for chunk in chunks]
            self._df = Counter()
            for stats in self._stats:
                self._df.update(set(stats.heading) | set(stats.title) | set(stats.body))
            self._avg_len = (
                sum(stats.weighted_len for stats in self._stats) / len(self._stats)
                if self._stats else 1.0
            ) or 1.0

    def list_docs(self) -> list[dict[str, Any]]:
        self._load()
        assert self._docs is not None
        return [{"slug": e.slug, "title": e.title, "description": e.description,
                 "uri": f"platform://docs/{e.slug}", "path": e.path}
                for e in self._docs]

    def _entry(self, slug: str) -> DocEntry | None:
        self._load()
        assert self._docs is not None
        return next((d for d in self._docs if d.slug == slug), None)

    def read(self, slug: str, max_chars: int = 50000) -> dict[str, Any] | None:
        self._load()
        item = self._raw.get(str(slug or "").strip())
        if not item:
            return None
        entry, text = item
        try:
            limit = max(1000, min(80000, int(max_chars or 50000)))
        except (TypeError, ValueError):
            limit = 50000
        clipped = len(text) > limit
        return {"slug": entry.slug, "title": entry.title, "path": entry.path,
                "text": text[:limit], "truncated": clipped,
                "total_chars": len(text)}

    def _bm25f(self, index: int, query_terms: list[str]) -> float:
        assert self._stats is not None
        stats = self._stats[index]
        total = len(self._stats) or 1
        score = 0.0
        for term in query_terms:
            tf = (
                FIELD_WEIGHTS["heading"] * stats.heading.get(term, 0)
                + FIELD_WEIGHTS["title"] * stats.title.get(term, 0)
                + FIELD_WEIGHTS["body"] * stats.body.get(term, 0)
            )
            if tf <= 0:
                continue
            df = max(1, self._df.get(term, 0))
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            norm = BM25_K1 * (1 - BM25_B + BM25_B * stats.weighted_len / self._avg_len)
            score += idf * tf * (BM25_K1 + 1) / (tf + norm)
        return score

    @staticmethod
    def _intent_boost(chunk: DocChunk, query: str) -> float:
        total = 0.0
        heading = chunk.heading.lower()
        for rule in _QUERY_RULES:
            if not rule.pattern.search(query):
                continue
            if chunk.slug != rule.slug:
                continue
            if rule.heading_terms and not any(term.lower() in heading
                                              for term in rule.heading_terms):
                continue
            total += rule.boost
        return min(total, 36.0)

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        self._load()
        assert self._chunks is not None and self._stats is not None
        q = _normalize(query)
        if not q:
            return []
        try:
            limit = max(1, min(10, int(limit)))
        except (TypeError, ValueError):
            limit = 5
        query_terms = _query_terms(query)
        exact_ids = _exact_identifiers(query)
        scored: list[tuple[float, DocChunk]] = []
        for index, chunk in enumerate(self._chunks):
            score = self._bm25f(index, query_terms)
            matched = score > 0
            body = chunk.text.lower()
            heading = chunk.heading.lower()
            title = chunk.doc_title.lower()

            # 完整 query 精确命中仍是强信号；BM25 负责词项覆盖和长度归一化。
            if len(q) >= 2:
                if q in heading:
                    score += 8.0
                    matched = True
                elif q in body:
                    score += 4.0
                    matched = True
                elif q in title:
                    score += 5.0
                    matched = True

            for ident in exact_ids:
                low = ident.lower()
                if low in heading:
                    score += 8.0
                    matched = True
                elif low in body:
                    score += 5.0
                    matched = True

            # 标题覆盖度：章节主题命中优先于正文偶然出现。
            heading_hits = sum(1 for term in query_terms if term in heading)
            score += min(6.0, heading_hits * 1.2)
            intent_boost = self._intent_boost(chunk, q)
            if intent_boost:
                matched = True
            if not matched:
                continue
            score += intent_boost
            score += DOC_PRIORITY.get(chunk.slug, 0)
            # 操作意图优先：问“新建诊断”时，“发起一次诊断”应压过助手/MCP 介绍。
            if any(k in q for k in ("新建诊断", "发起诊断", "创建诊断")):
                if "平台助手" in chunk.heading:
                    score -= 14.0
            # 标题块比正文块更有导航价值，轻微加权。
            if chunk.heading == chunk.doc_title:
                score += 1.5
            # 版本历史/变更记录通常不是操作说明，避免压过使用手册。
            if re.search(r"版本|历史|本轮变化|更新记录|变更|功能更新", chunk.heading):
                if not any(word in q for word in ("版本", "1.20", "1.19", "变更", "历史")):
                    score -= 6.0
            scored.append((score, chunk))

        scored.sort(key=lambda item: (-item[0], item[1].slug, item[1].order))
        results: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for score, chunk in scored:
            key = (chunk.slug, chunk.heading)
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "slug": chunk.slug, "doc_title": chunk.doc_title,
                "heading": chunk.heading, "score": round(score, 1),
                "excerpt": _excerpt(chunk.text, query),
                "uri": f"platform://docs/{chunk.slug}",
            })
            if len(results) >= limit:
                break
        return results

    def prompt_context(self, limit: int = 5) -> str:
        """给提示词用的文档目录，不含正文。"""
        docs = self.list_docs()
        return "\n".join(f"- {d['title']}（{d['slug']}）：{d['description']}"
                         for d in docs[:limit])
