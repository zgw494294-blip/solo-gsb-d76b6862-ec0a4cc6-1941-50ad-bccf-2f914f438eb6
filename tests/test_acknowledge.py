"""POST /v1/acknowledge 端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app
from test_verify import build_edi, isa

client = TestClient(app)

CONTROLS = {"isa13": "000000777", "gs06": "77", "st02": "7777"}


def post_ack(edi_text, **controls):
    return client.post("/v1/acknowledge",
                       json={"edi_text": edi_text, **CONTROLS, **controls})


def ack_segments(ack, term="~"):
    """按段终止符切分 997，去掉终止符后的空串。"""
    return [s for s in ack.split(term) if s]


def tags_of(segs, elem="*"):
    return [s.split(elem)[0] for s in segs]


def check_self_consistent(ack, *, elem="*", term="~",
                          isa13="000000777", gs06="77", st02="7777"):
    """校验 997 外壳自洽：ISA 定长、收发方互换、控制号配对与计数。"""
    segs = ack_segments(ack, term)
    f = segs[0].split(elem)
    assert len(segs[0]) == 105               # ISA 固定 105 字节（不含终止符）
    assert f[6] == "RECEIVER1234567"         # ISA06/ISA08 互换
    assert f[8] == "SENDER123456789"
    assert f[13] == isa13
    g = segs[1].split(elem)
    assert g[1] == "FA" and g[6] == gs06 and g[8] == "004010"
    st = segs[2].split(elem)
    assert st[1] == "997" and st[2] == st02
    se = segs[-3].split(elem)
    assert se[0] == "SE"
    assert int(se[1]) == len(segs) - 4       # SE01 = ST..SE 实际段数
    assert se[2] == st02                     # SE02 == ST02
    assert segs[-2].split(elem) == ["GE", "1", gs06]    # GE01=1, GE02==GS06
    assert segs[-1].split(elem) == ["IEA", "1", isa13]  # IEA01=1, IEA02==ISA13
    return segs


# ---------- 接受路径 ----------

def test_accept_valid_invoice():
    r = post_ack(build_edi([("1", "2", "10.50"), ("2", "3", "4.25")]))
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    segs = check_self_consistent(body["ack_edi"])
    assert tags_of(segs) == ["ISA", "GS", "ST", "AK1", "AK2",
                             "AK5", "AK9", "SE", "GE", "IEA"]
    assert segs[3] == "AK1*IN*42"            # 引用来报 GS06
    assert segs[4] == "AK2*810*0001"         # 引用来报 ST02
    assert segs[5] == "AK5*A"
    assert segs[6] == "AK9*A*1"              # 接收数为 1
    assert segs[7] == "SE*6*7777"


def test_sender_receiver_swapped():
    body = post_ack(build_edi([("1", "1", "1.00")])).json()
    segs = ack_segments(body["ack_edi"])
    f = segs[0].split("*")
    assert f[5] == "ZZ" and f[7] == "ZZ"     # 限定符随收发方一起互换
    assert f[6] == "RECEIVER1234567" and f[8] == "SENDER123456789"
    g = segs[1].split("*")
    assert g[2] == "RECVGS" and g[3] == "SENDERGS"   # GS02/GS03 互换


def test_ak1_ak2_reference_incoming_controls():
    edi = build_edi([("1", "1", "1.00")], gs_ctrl="4242", st_ctrl="9876")
    segs = ack_segments(post_ack(edi).json()["ack_edi"])
    assert "AK1*IN*4242" in segs
    assert "AK2*810*9876" in segs


def test_ack_reuses_incoming_delimiters():
    edi = build_edi([("1", "2", "1.00")], elem="|", comp="^", term="#")
    r = post_ack(edi)
    assert r.status_code == 200
    ack = r.json()["ack_edi"]
    assert ack.startswith("ISA|") and ack.endswith("#")
    segs = check_self_consistent(ack, elem="|", term="#")
    assert segs[0].split("|")[16] == "^"     # 组件分隔符沿用
    assert "AK1|IN|42" in segs
    assert "SE|6|7777" in segs


def test_accept_big_amount_invoice():
    # 与 /v1/verify 相同的整数定点核验：29 位数量也能通过
    qty = "12345678901234567890123456789"
    body = post_ack(build_edi([("1", qty, "1")])).json()
    assert body["accepted"] is True


# ---------- 拒绝路径（仍生成 997） ----------

def test_reject_tds_mismatch_writes_ak3():
    edi = build_edi([("1", "2", "10.00")], tds="1999")
    r = post_ack(edi)
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is False
    segs = check_self_consistent(body["ack_edi"])
    assert tags_of(segs) == ["ISA", "GS", "ST", "AK1", "AK2", "AK3",
                             "AK5", "AK9", "SE", "GE", "IEA"]
    # TDS 为来报第 6 段（ISA=1），从 ST 起算位置 4
    assert segs[5] == "AK3*TDS*4"
    assert segs[6] == "AK5*R"
    assert segs[7] == "AK9*R*0"              # 接收数为 0
    assert segs[8] == "SE*7*7777"            # AK3 计入 SE01


def test_ak3_position_counted_from_st():
    big_empty = isa() + (
        "GS*IN*A*B*20200830*1200*42*X*004010~"
        "ST*810*0001~BIG*20200830*~"
        "IT1*1*1*EA*1.00~TDS*100~SE*5*0001~GE*1*42~IEA*1*000000001~"
    )
    cases = [
        (build_edi([("1", "0", "1.00")]), "AK3*IT1*3"),   # IT102_INVALID
        (big_empty, "AK3*BIG*2"),                          # BIG02_EMPTY
        (build_edi([("1", "1", "1.00")]).replace("ST*810*", "ST*820*"),
         "AK3*ST*1"),                                      # ST01_NOT_810
    ]
    for edi, expected_ak3 in cases:
        body = post_ack(edi).json()
        assert body["accepted"] is False
        assert expected_ak3 in ack_segments(body["ack_edi"])


def test_envelope_version_errors_acknowledged_without_ak3():
    # ISA12/GS08 版本错误：外壳可解析，仍生成 997；错误段不在交易集内，无 AK3
    edi = build_edi([("1", "1", "1.00")])
    for bad in (edi.replace("*00401*", "*00501*"),
                edi.replace("*004010~", "*005010X222~")):
        body = post_ack(bad).json()
        assert body["accepted"] is False
        segs = check_self_consistent(body["ack_edi"])
        assert "AK3" not in tags_of(segs)
        assert "AK5*R" in segs and "AK9*R*0" in segs


# ---------- 422：来报外壳不足以生成回执（带原错误码） ----------

def expect_422(edi, code, **controls):
    r = post_ack(edi, **controls)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == code


def test_422_shell_not_parseable():
    expect_422("", "EMPTY_INPUT")
    expect_422("café", "NOT_ASCII")
    expect_422("XX" + isa()[2:], "ISA_NOT_FIRST")
    expect_422(isa()[:50], "ISA_TOO_SHORT")
    expect_422(isa(elem="A"), "INVALID_ELEMENT_SEPARATOR")


def test_422_missing_ge_iea():
    # 截断：无 GE/IEA 结束段
    edi = isa() + "GS*IN*A*B*20200830*1200*42*X*004010~ST*810*0001~"
    expect_422(edi, "MISSING_SEGMENT")


def test_422_trailing_data():
    expect_422(build_edi([("1", "1", "1.00")]) + "GARBAGE", "TRAILING_DATA")


def test_422_gs06_not_extractable():
    # GS 只有 6 个元素（无 GS06）
    edi = isa() + (
        "GS*IN*A*B*20200830*1200~"
        "ST*810*0001~BIG*20200830*9~IT1*1*1*EA*1.00~TDS*100~"
        "SE*5*0001~GE*1*~IEA*1*000000001~"
    )
    expect_422(edi, "SEGMENT_TOO_FEW_ELEMENTS")


# ---------- 422：回执控制号非法 ----------

def test_422_invalid_isa13():
    good = build_edi([("1", "1", "1.00")])
    for bad in ["", "123", "abcdefghi", "0000000001", "00000 001"]:
        expect_422(good, "ACK_ISA13_INVALID", isa13=bad)


def test_422_empty_gs06_st02():
    good = build_edi([("1", "1", "1.00")])
    expect_422(good, "ACK_GS06_EMPTY", gs06="")
    expect_422(good, "ACK_ST02_EMPTY", st02="")


def test_422_control_checked_before_edi():
    # 控制号与来报均非法时，先报控制号错误
    expect_422("garbage", "ACK_ISA13_INVALID", isa13="x")


def test_422_control_contains_delimiter():
    good = build_edi([("1", "1", "1.00")])
    expect_422(good, "ACK_GS06_INVALID", gs06="1*2")
    expect_422(good, "ACK_ST02_INVALID", st02="1~2")


# ---------- 请求形状 ----------

def test_missing_fields_422():
    assert client.post("/v1/acknowledge", json={}).status_code == 422
    r = client.post("/v1/acknowledge", json={"edi_text": "x"})
    assert r.status_code == 422
