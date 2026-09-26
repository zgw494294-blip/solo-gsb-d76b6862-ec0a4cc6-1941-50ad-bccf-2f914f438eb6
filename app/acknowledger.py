"""X12 997 功能确认（Functional Acknowledgment）回执生成。

POST /v1/acknowledge 的核心逻辑：接收单份 810 报文与回执控制号
（九位数字 ISA13、非空 GS06/ST02），返回 004010 X12 997 回执报文。

仅当来报外壳可解析（ISA 结构/分隔符/切段正常）、含 GE/IEA 结束段、且收发方
及 GS06/ST02 可提取时才生成回执；否则抛出来报核验的原 X12Error（HTTP 层
返回带原错误码的 422）。

回执规则：
  * 沿用来报的元素/组件/段终止分隔符；ISA05/06 与 ISA07/08、GS02/GS03
    互换（收发方对调）；
  * AK1/AK2 分别引用来报的 GS06/ST02；
  * 复用 verify_text 的发票核验：通过 -> AK5/AK9 为 A、AK9 接收数为 1；
    失败 -> AK5/AK9 为 R、接收数为 0；错误段可定位到 ST 之后（含 ST）时用
    AK3 写段标识及从 ST 起算（ST=1）的位置；
  * 回执 ISA13/GS06/ST02 取自请求参数；SE01 段数、GE01/IEA01 计数与控制号
    配对（ST02=SE02、GS06=GE02、ISA13=IEA02）均自洽。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .verifier import X12Error, parse_envelope, verify_text

_ISA13_RE = re.compile(r"^\d{9}$")


class AckError(Exception):
    """回执控制号非法；整个请求返回 422。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AckEnvelope:
    """从来报提取的、生成 997 所需的外壳信息。"""

    element_sep: str
    segment_term: str
    isa_fields: list[str]  # ["ISA", ISA01..ISA16]
    gs01: str              # 来报功能组标识（如 IN）
    gs02: str              # 来报应用发送方代码
    gs03: str              # 来报应用接收方代码
    gs04: str              # 来报组日期
    gs05: str              # 来报组时间
    gs06: str              # 来报组控制号
    st01: str              # 来报交易集标识（810）
    st02: str              # 来报交易集控制号


def extract_envelope(edi_text: str) -> Optional[AckEnvelope]:
    """扫描来报外壳；返回 None 表示不足以生成 997（调用方回 422）。

    判定条件：ISA 外壳可解析、无尾随数据、首段为 GS 且 GS06 可提取、
    次段为 ST 且 ST02 可提取、最后两段为 GE/IEA 结束段。
    ISA12 版本不属于外壳结构问题，不在此检查。
    """
    try:
        env = parse_envelope(edi_text, check_isa12=False)
    except X12Error:
        return None
    if env.trailing_data is not None:
        return None
    segs = env.segments
    if len(segs) < 4:                    # 至少 GS、ST、GE、IEA
        return None
    _, gs_tag, gs = segs[0]
    if gs_tag != "GS" or len(gs) < 7:    # GS06 不可提取
        return None
    _, st_tag, st = segs[1]
    if st_tag != "ST" or len(st) < 3:    # ST02 不可提取
        return None
    if segs[-2][1] != "GE" or segs[-1][1] != "IEA":  # 缺 GE/IEA 结束段
        return None
    return AckEnvelope(
        element_sep=env.element_sep,
        segment_term=env.segment_term,
        isa_fields=env.isa_fields,
        gs01=gs[1], gs02=gs[2], gs03=gs[3], gs04=gs[4], gs05=gs[5],
        gs06=gs[6],
        st01=st[1], st02=st[2],
    )


def acknowledge(edi_text: str, isa13: str, gs06: str,
                st02: str) -> tuple[str, bool]:
    """生成 997 回执，返回 (回执报文, 来报是否通过核验)。

    回执控制号非法抛 AckError；来报外壳不足以生成回执时抛出来报核验的
    X12Error（保留原错误码）。
    """
    _validate_controls(isa13, gs06, st02)

    verify_error: Optional[X12Error] = None
    try:
        verify_text(edi_text)
    except X12Error as exc:
        verify_error = exc

    env = extract_envelope(edi_text)
    if env is None:
        # 外壳不可解析/缺 GE/IEA/GS06/ST02 不可提取：核验必然已失败
        assert verify_error is not None
        raise verify_error

    # 控制号不得含来报分隔符，否则回执无法自洽
    for value, code, name in ((gs06, "ACK_GS06_INVALID", "GS06"),
                              (st02, "ACK_ST02_INVALID", "ST02")):
        if env.element_sep in value or env.segment_term in value:
            raise AckError(
                code, f"回执 {name} 含有来报分隔符，无法生成回执: {value!r}")

    accepted = verify_error is None
    return _build_997(env, isa13, gs06, st02, accepted, verify_error), accepted


def _validate_controls(isa13: str, gs06: str, st02: str) -> None:
    if not _ISA13_RE.fullmatch(isa13):
        raise AckError("ACK_ISA13_INVALID",
                       f"回执 ISA13 必须为九位数字: {isa13!r}")
    if gs06 == "":
        raise AckError("ACK_GS06_EMPTY", "回执 GS06 不能为空")
    if st02 == "":
        raise AckError("ACK_ST02_EMPTY", "回执 ST02 不能为空")


def _build_997(env: AckEnvelope, isa13: str, gs06: str, st02: str,
               accepted: bool, error: Optional[X12Error]) -> str:
    e = env.element_sep
    t = env.segment_term

    def seg(*fields: str) -> str:
        return e.join(fields) + t

    # ISA：沿用来报各字段，互换收发方（ISA05/06 <-> ISA07/08），写入回执 ISA13
    isa = list(env.isa_fields)
    isa[5], isa[7] = isa[7], isa[5]
    isa[6], isa[8] = isa[8], isa[6]
    isa[13] = isa13

    code = "A" if accepted else "R"
    received = "1" if accepted else "0"

    body = [
        seg("ST", "997", st02),
        seg("AK1", env.gs01, env.gs06),   # 引用来报 GS06
        seg("AK2", env.st01, env.st02),   # 引用来报 ST02
    ]
    if not accepted and error is not None and error.segment is not None \
            and error.seg is not None:
        position = error.seg - 2          # 从 ST 起算（ST=1）
        if position >= 1:                 # ISA/GS 等外壳段不在交易集内，不写 AK3
            body.append(seg("AK3", error.segment, str(position)))
    body.append(seg("AK5", code))
    body.append(seg("AK9", code, received))
    # SE01 = ST..SE 段数（含 ST 与 SE）
    body.append(seg("SE", str(len(body) + 1), st02))

    return "".join([
        e.join(isa) + t,
        seg("GS", "FA", env.gs03, env.gs02, env.gs04, env.gs05,
            gs06, "X", "004010"),
        *body,
        seg("GE", "1", gs06),
        seg("IEA", "1", isa13),
    ])
