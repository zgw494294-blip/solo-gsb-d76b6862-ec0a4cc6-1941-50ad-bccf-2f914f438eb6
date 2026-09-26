"""ASC X12 004010 997 Functional Acknowledgment（功能回执）生成。

入口：acknowledge(edi_text, isa13, gs06, st02) -> 997 报文文本。

仅当满足以下外壳前提时才生成回执，否则抛出来报核验的原始 X12Error（HTTP 422）：
  * 通过与 /v1/verify 相同的 ASCII/大小预检；
  * ISA 段可解析（106 字节、分隔符合法且互不冲突、ISA12 == "00401"、
    16 个元素齐全，收发方限定符/标识可提取）；
  * 恰为一份交换/功能组/交易集，GS/ST 在前、SE/GE/IEA 正常收尾，
    中间夹有交易集正文；
  * GS08 == "004010"、GS06 非空，ST01 == "810"、ST02 非空。

业务核验直接复用 verifier.verify_text：
  * 通过 -> AK5/AK9 = A，AK9 接收数 1，不输出 AK3；
  * 失败 -> AK5/AK9 = R，AK9 接收数 0；错误可定位到段（X12Error 同时带
    seg 与段标识）时输出 AK3，AK302 为从 ST 起算的位置（ST = 1）。

回执沿用来报的三种分隔符，互换收发方（ISA05/06 <-> ISA07/08、
GS02 <-> GS03），AK1/AK2 分别引用来报 GS01/GS06 与 810/ST02；
回执自身的 ST02/SE02、GS06/GE02、ISA13/IEA02 配对，SE01 段数与
GE01/IEA01 计数均自洽。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .verifier import MAX_BYTES, X12Error, verify_text

_NINE_DIGITS_RE = re.compile(r"^\d{9}$")

# 仅允许一份交换/功能组/交易集：正文中不允许再出现信封段标识
_ENVELOPE_TAGS = frozenset({"ISA", "GS", "ST", "GE", "IEA"})


@dataclass(frozen=True)
class _Shell:
    """从可回执来报中提取的外壳信息。"""

    element_sep: str
    component_sep: str
    segment_term: str
    isa_fields: list[str]   # 105 字节 ISA 片段按元素分隔符切出的 17 个字段（末字段即 ISA16）
    gs_fields: list[str]    # GS 段元素（含段标识，至少 9 个）
    st_ctrl: str            # 来报 ST02


def _extract_shell(edi_text: str) -> Optional[_Shell]:
    """宽容外壳提取：满足回执前提返回 _Shell，否则返回 None。

    本函数不定义错误码（不可回执时由调用方抛出来报核验的原始 X12Error），
    对任意畸形输入都不得抛出异常。
    """
    try:
        raw = edi_text.encode("ascii")
    except UnicodeEncodeError:
        return None
    if not raw or len(raw) > MAX_BYTES:
        return None

    try:
        # ---- ISA：与 verifier 完全相同的分隔符/结构规则 -----------------
        if not raw.startswith(b"ISA") or len(raw) < 106:
            return None
        element_sep = chr(raw[3])
        component_sep = chr(raw[104])
        segment_term = chr(raw[105])

        if element_sep.isalnum() or element_sep in (" ", "\r", "\n", "\t"):
            return None
        if segment_term.isalnum() or segment_term in (" ", "\t"):
            return None
        if segment_term == element_sep:
            return None
        if component_sep.isalnum() or component_sep in (" ", "\r", "\n", "\t"):
            return None
        if component_sep in (element_sep, segment_term):
            return None

        isa_fields = raw[:105].decode("ascii").split(element_sep)
        if len(isa_fields) != 17 or any(f == "" for f in isa_fields):
            return None
        if isa_fields[12] != "00401":  # ISA12
            return None

        # ---- 切段：与 verifier 相同的尾随/空段处理 ----------------------
        fragments = raw[106:].decode("ascii").split(segment_term)
        if segment_term not in ("\r", "\n"):
            fragments = [f.strip("\r\n") for f in fragments]
        if not fragments or fragments[-1] != "":
            return None  # 未以段终止符收尾，或终止符后存在多余数据
        fragments.pop()

        segments: list[list[str]] = []
        for fragment in fragments:
            if fragment == "":
                return None  # 空段
            fields = fragment.split(element_sep)
            if fields[0] == "":
                return None
            segments.append(fields)

        # ---- 一份交换/功能组/交易集：GS/ST 在前，SE/GE/IEA 收尾 ----------
        if len(segments) < 5:
            return None
        gs, st = segments[0], segments[1]
        se, ge, iea = segments[-3], segments[-2], segments[-1]

        if gs[0] != "GS" or len(gs) < 9:
            return None
        if gs[8] != "004010" or gs[6] == "":  # GS08 / GS06
            return None
        if st[0] != "ST" or len(st) < 3:
            return None
        if st[1] != "810" or st[2] == "":     # ST01 / ST02
            return None
        if se[0] != "SE" or len(se) < 3:
            return None
        if ge[0] != "GE" or len(ge) < 3 or iea[0] != "IEA" or len(iea) < 3:
            return None
        for fields in segments[2:-3]:         # 交易集正文
            if fields[0] in _ENVELOPE_TAGS:
                return None

        return _Shell(element_sep=element_sep,
                      component_sep=component_sep,
                      segment_term=segment_term,
                      isa_fields=isa_fields,
                      gs_fields=gs,
                      st_ctrl=st[2])
    except Exception:
        return None


def _control_value_error(name: str, value: str, shell: _Shell) -> Optional[X12Error]:
    """校验回执 GS06/ST02：非空、ASCII、不含分隔符或控制字符。"""
    label = name.upper()
    if value == "":
        return X12Error(f"ACK_{label}_EMPTY", f"回执 {label} 不能为空")
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return X12Error(f"ACK_{label}_INVALID",
                        f"回执 {label} 必须为纯 ASCII: {value!r}")
    forbidden = (shell.element_sep, shell.component_sep, shell.segment_term,
                 "\r", "\n")
    if any(ch in value for ch in forbidden) or any(ord(ch) < 0x20 or ch == "\x7f"
                                                   for ch in value):
        return X12Error(f"ACK_{label}_INVALID",
                        f"回执 {label} 含非法字符（分隔符或控制字符）: {value!r}")
    return None


def _build_997(shell: _Shell, accepted: bool, verr: Optional[X12Error],
               isa13: str, gs06: str, st02: str) -> str:
    """按外壳信息与核验结果组装自洽的 004010 X12 997。"""
    e = shell.element_sep
    term = shell.segment_term

    def seg(tag: str, *values: str) -> str:
        return e.join([tag, *values]) + term

    # ---- ISA：原样复制各字段，互换收发方限定符/标识，替换 ISA13 ----------
    # isa_fields[0] 为 "ISA"，isa_fields[n] 即 ISAn（ISA16 为组件分隔符）
    old = shell.isa_fields
    new_isa = old.copy()
    new_isa[5], new_isa[6] = old[7], old[8]   # 来报接收方 -> 回执发送方
    new_isa[7], new_isa[8] = old[5], old[6]   # 来报发送方 -> 回执接收方
    new_isa[13] = isa13
    isa_segment = e.join(new_isa) + term
    assert len(isa_segment) == 106  # 等宽字段互换 + 9 位控制号，ISA 仍为 106 字节

    g = shell.gs_fields
    out: list[str] = [
        isa_segment,
        # GS01=FA；GS02/03 互换；GS04/05/07 沿用；GS06 回执控制号；GS08=004010
        seg("GS", "FA", g[3], g[2], g[4], g[5], gs06, g[7], "004010"),
        seg("ST", "997", st02),
        seg("AK1", g[1], g[6]),                # 来报 GS01 功能标识、GS06 组控制号
        seg("AK2", "810", shell.st_ctrl),      # 来报交易集类型与 ST02
    ]

    if not accepted and verr is not None and verr.seg is not None and verr.segment:
        # verifier 的段序号：ISA=1、GS=2、ST=3；从 ST 起算 ST=1 -> seg-2
        position = verr.seg - 2
        if position >= 1:
            out.append(seg("AK3", verr.segment, str(position)))

    code = "A" if accepted else "R"
    out.append(seg("AK5", code))
    out.append(seg("AK9", code, "1" if accepted else "0"))  # 接收数：1 / 0

    # SE01 = ST..SE（含两端）的段数；ST 在 out 中索引为 2
    se_count = len(out) - 2 + 1
    out.append(seg("SE", str(se_count), st02))
    out.append(seg("GE", "1", gs06))
    out.append(seg("IEA", "1", isa13))
    return "".join(out)


def acknowledge(edi_text: str, isa13: str, gs06: str, st02: str) -> str:
    """生成 997 回执；外壳不满足前提抛原核验 X12Error，控制号非法抛 ACK_* 错误。"""
    # 复用现有发票核验：决定 A/R（失败时保留错误以定位 AK3 段）
    try:
        verify_text(edi_text)
        verr: Optional[X12Error] = None
    except X12Error as exc:
        verr = exc

    shell = _extract_shell(edi_text)
    if shell is None:
        if verr is not None:
            raise verr
        # 理论不可达：能通过 verify_text 的报文必然满足外壳前提
        raise X12Error("ACK_ENVELOPE_UNUSABLE",
                       "来报外壳不可解析或收发方/GS06/ST02 无法提取，不能生成回执")

    # 回执控制号校验（外壳已知分隔符，可一并拒绝分隔符字符）
    if not _NINE_DIGITS_RE.fullmatch(isa13):
        raise X12Error(
            "ACK_ISA13_INVALID",
            f"回执 ISA13 必须为恰好 9 位数字: {isa13!r}")
    for name, value in (("GS06", gs06), ("ST02", st02)):
        err = _control_value_error(name, value, shell)
        if err is not None:
            raise err

    return _build_997(shell, verr is None, verr, isa13, gs06, st02)
