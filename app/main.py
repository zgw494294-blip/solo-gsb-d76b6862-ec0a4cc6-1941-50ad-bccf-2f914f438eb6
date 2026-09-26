"""X12 810 发票核验、采购单核销与 997 功能回执 HTTP API。"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .acknowledger import acknowledge
from .reconciler import (
    MAX_EDI_TEXTS,
    OrderError,
    milli_to_str,
    reconcile,
    validate_order_lines,
)
from .verifier import X12Error, verify_text

app = FastAPI(
    title="X12 810 Invoice Verification API",
    version="1.2.0",
    description="核验 ASC X12 004010 单份 810 发票报文的外壳结构与发票金额，"
                "按采购单对多份发票逐张核销，并对单份 810 生成 997 功能回执。",
)


class VerifyRequest(BaseModel):
    edi_text: str = Field(..., description="完整 X12 810 报文（ASCII，≤64 KiB）")


class LineOut(BaseModel):
    line_id: str = Field(..., description="IT101 行号")
    quantity: str = Field(..., description="IT102 数量（原文）")
    unit_price: str = Field(..., description="IT104 单价（原文）")
    line_amount: int = Field(..., description="行金额，整数分（ROUND_HALF_UP）")


class VerifyResponse(BaseModel):
    invoice_number: str = Field(..., description="BIG02 发票号")
    lines: list[LineOut]
    total_amount: int = Field(..., description="TDS01 总额，整数分")


@app.exception_handler(X12Error)
async def x12_error_handler(_request, exc: X12Error) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "seg": exc.seg,
                "segment": exc.segment,
            }
        },
    )


@app.exception_handler(OrderError)
async def order_error_handler(_request, exc: OrderError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.post(
    "/v1/verify",
    response_model=VerifyResponse,
    summary="核验一份 X12 810 发票报文",
)
async def verify_invoice(req: VerifyRequest) -> VerifyResponse:
    invoice = verify_text(req.edi_text)
    return VerifyResponse(
        invoice_number=invoice.invoice_number,
        lines=[
            LineOut(
                line_id=line.line_id,
                quantity=line.quantity,
                unit_price=line.unit_price,
                line_amount=line.line_amount,
            )
            for line in invoice.lines
        ],
        total_amount=invoice.total_amount,
    )


# ---------- POST /v1/reconcile ----------


class OrderLineInModel(BaseModel):
    line_id: str = Field(..., description="采购单行号，须唯一")
    ordered_quantity: str = Field(
        ..., description="订购数量（十进制字符串，正，至多 3 位小数）")
    unit_price: str = Field(
        ..., description="单价（十进制字符串，非负，至多 4 位小数）")


class ReconcileRequest(BaseModel):
    order_lines: list[OrderLineInModel] = Field(..., description="采购单行")
    edi_texts: list[str] = Field(
        ..., min_length=1, max_length=MAX_EDI_TEXTS,
        description=f"按输入顺序核销的 1～{MAX_EDI_TEXTS} 份 X12 810 报文")


class RejectOut(BaseModel):
    code: str = Field(..., description="拒绝原因错误码（核验失败时为原核验错误码）")
    message: str
    seg: Optional[int] = Field(None, description="原核验错误的段序号（仅核验失败时）")
    segment: Optional[str] = Field(None, description="原核验错误的段标识（仅核验失败时）")
    line_id: Optional[str] = Field(None, description="触发拒绝的采购单行号（核销失败时）")


class InvoiceResultOut(BaseModel):
    index: int = Field(..., description="edi_texts 中的输入序号（从 0 开始）")
    accepted: bool
    invoice_number: Optional[str] = Field(
        None, description="BIG02 发票号；核验失败时为 null")
    error: Optional[RejectOut] = None


class RemainingOut(BaseModel):
    line_id: str = Field(..., description="采购单行号")
    remaining_quantity: str = Field(..., description="剩余数量（十进制字符串）")


class ReconcileResponse(BaseModel):
    results: list[InvoiceResultOut] = Field(..., description="逐张核销结果")
    remaining: list[RemainingOut] = Field(
        ..., description="各采购单行剩余数量（按采购单顺序）")


@app.post(
    "/v1/reconcile",
    response_model=ReconcileResponse,
    summary="按采购单对多份 X12 810 发票逐张核销",
)
async def reconcile_invoices(req: ReconcileRequest) -> ReconcileResponse:
    # 采购单非法 -> 整单 422
    order_lines = validate_order_lines(req.order_lines)
    outcome = reconcile(order_lines, req.edi_texts)
    return ReconcileResponse(
        results=[
            InvoiceResultOut(
                index=r.index,
                accepted=r.accepted,
                invoice_number=r.invoice_number,
                error=RejectOut(
                    code=r.error.code,
                    message=r.error.message,
                    seg=r.error.seg,
                    segment=r.error.segment,
                    line_id=r.error.line_id,
                ) if r.error is not None else None,
            )
            for r in outcome.results
        ],
        remaining=[
            RemainingOut(line_id=line_id,
                         remaining_quantity=milli_to_str(milli))
            for line_id, milli in outcome.remaining
        ],
    )


# ---------- POST /v1/acknowledge ----------


class AcknowledgeRequest(BaseModel):
    edi_text: str = Field(..., description="完整 X12 810 报文（ASCII，≤64 KiB）")
    isa13: str = Field(..., description="回执 ISA13 交换控制号（恰好 9 位数字）")
    gs06: str = Field(..., description="回执 GS06 组控制号（非空，不含分隔符）")
    st02: str = Field(..., description="回执 ST02 交易集控制号（非空，不含分隔符）")


class AcknowledgeResponse(BaseModel):
    edi_text: str = Field(..., description="ASC X12 004010 997 功能回执报文")


@app.post(
    "/v1/acknowledge",
    response_model=AcknowledgeResponse,
    summary="对单份 X12 810 报文生成 997 功能回执",
)
async def acknowledge_invoice(req: AcknowledgeRequest) -> AcknowledgeResponse:
    return AcknowledgeResponse(
        edi_text=acknowledge(req.edi_text, req.isa13, req.gs06, req.st02))


@app.get("/health", summary="存活探针")
async def health() -> dict:
    return {"status": "ok"}
