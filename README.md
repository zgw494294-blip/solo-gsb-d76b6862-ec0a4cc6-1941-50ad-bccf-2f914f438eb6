# X12 810 发票核验 API

基于 FastAPI 的 ASC X12 810 Invoice（版本 `ISA12=00401` / `GS08=004010`）报文核验服务。
接收**单份**交换/功能组/交易集，校验信封结构、控制号配对与发票金额合计，
成功返回发票号、各行金额与总额（均为整数分）；任何核验失败返回 `422`，
错误体带**段序号**与**错误码**。

另提供 `POST /v1/reconcile`：给定一张采购单，对 1～50 份发票报文按输入顺序
逐张核销（逐张核验、行号/单价比对、数量累计），返回逐张接受状态与各采购单行
剩余数量。

## 单条命令启动

需要 Docker（含 Compose 插件）：

```bash
docker compose up --build -d
```

- 服务地址：`http://localhost:8080`
- 核验接口：`POST http://localhost:8080/v1/verify`
- 核销接口：`POST http://localhost:8080/v1/reconcile`
- 健康检查：`GET http://localhost:8080/health`
- 交互文档（Swagger UI）：`http://localhost:8080/docs`

停止：`docker compose down`

## 请求 / 响应（POST /v1/verify）

请求体（JSON）：

```json
{
  "edi_text": "ISA*00*          *00*          *..."
}
```

`edi_text` 必须为纯 ASCII，大小不超过 **64 KiB（65536 字节）**。

成功 `200`：

```json
{
  "invoice_number": "INV-2020-0830",
  "lines": [
    {"line_id": "1", "quantity": "10", "unit_price": "19.99", "line_amount": 19990},
    {"line_id": "2", "quantity": "3",  "unit_price": "0.05",  "line_amount": 15}
  ],
  "total_amount": 20005
}
```

失败 `422`（`seg` 为从 1 开始的段序号；与具体段无关的错误为 `null`）：

```json
{
  "error": {
    "code": "TDS_MISMATCH",
    "message": "TDS01 总额 20000 分与各行金额之和 20005 分不一致",
    "seg": 7,
    "segment": "TDS"
  }
}
```

调用示例：

```bash
curl -s http://localhost:8080/v1/verify \
  -H 'Content-Type: application/json' \
  -d '{"edi_text": "ISA*00*          *00*          *ZZ*SENDER123456789*ZZ*RECEIVER1234567*000830*1200*U*00401*000000001*0*P*:~GS*IN*S*R*20200830*1200*42*X*004010~ST*810*0001~BIG*20200830*INV1~IT1*1*10*EA*19.99~TDS*19990~SE*5*0001~GE*1*42~IEA*1*000000001~"}'
```

## 请求 / 响应（POST /v1/reconcile）

按一张采购单对 **1～50 份**发票报文（`edi_texts`，按输入顺序）逐张核销。

请求体（JSON）：

```json
{
  "order_lines": [
    {"line_id": "1", "ordered_quantity": "10", "unit_price": "19.99"},
    {"line_id": "2", "ordered_quantity": "5",  "unit_price": "4.25"}
  ],
  "edi_texts": [
    "ISA*00*          *00*          *ZZ*SENDER123456789*ZZ*RECEIVER1234567*000830*1200*U*00401*000000001*0*P*:~GS*IN*SENDERGS*RECVGS*20200830*1200*42*X*004010~ST*810*0001~BIG*20200830*INV-1~IT1*1*2*EA*19.99~IT1*2*1*EA*4.25~TDS*4423~SE*6*0001~GE*1*42~IEA*1*000000001~",
    "ISA*00*          ...第二份报文..."
  ]
}
```

- `order_lines`：采购单行。`line_id` 须唯一；`ordered_quantity` 为十进制字符串，
  必须为正、至多 3 位小数；`unit_price` 为十进制字符串，非负、至多 4 位小数。
  **采购单非法时整个请求返回 `422`**（错误体为 `{"error": {"code", "message"}}`）。
- `edi_texts`：1～50 份报文，每份要求同 `/v1/verify`（纯 ASCII、≤64 KiB）。

成功 `200`（逐张结果 + 各采购单行剩余数量）：

```json
{
  "results": [
    {"index": 0, "accepted": true,  "invoice_number": "INV-1", "error": null},
    {"index": 1, "accepted": false, "invoice_number": null,
     "error": {"code": "TDS_MISMATCH",
               "message": "TDS01 总额 1 分与各行金额之和 1999 分不一致",
               "seg": 6, "segment": "TDS", "line_id": null}},
    {"index": 2, "accepted": false, "invoice_number": "INV-3",
     "error": {"code": "QUANTITY_EXCEEDED",
               "message": "行 '1' 累计数量 11 超出订购数量 10",
               "seg": null, "segment": null, "line_id": "1"}}
  ],
  "remaining": [
    {"line_id": "1", "remaining_quantity": "0"},
    {"line_id": "2", "remaining_quantity": "4"}
  ]
}
```

### 核销规则

1. 每份报文先做与 `/v1/verify` 完全相同的核验（ASCII、64 KiB、X12 结构与金额）。
   **核验失败只拒绝该张**，`error` 中原样带回核验错误码、`seg`、`segment`，
   后续报文继续处理。
2. 通过核验的发票按 `IT101` 匹配采购单行号，`IT104` 与采购单单价按**十进制数值**
   精确比较（`"1.5"` 与 `"1.50"` 视为同一价格），`IT102` 逐行累计到对应采购单行。
3. 出现以下任一情况时**拒绝整张发票，且不占用任何数量**（后续发票继续按输入
   顺序核销）：
   - 发票行号不在采购单中（`PO_LINE_NOT_FOUND`）；
   - 单价与采购单不符（`UNIT_PRICE_MISMATCH`）；
   - 发票号（`BIG02`）与**已接受**的发票重复（`INVOICE_DUPLICATE`；
     被拒绝的发票不占用发票号，同号重发可被接受）；
   - 任一采购单行累计数量超出订购数量（`QUANTITY_EXCEEDED`；恰好等于订购
     数量时接受）。
4. 数量一律以 10⁻³ 整数（毫单位）累计、单价以 10⁻⁴ 整数比较，**全程不使用
   浮点数**；`remaining_quantity` 以最简十进制字符串返回（如 `"7.5"`、`"0"`）。

### 核销错误码（采购单 422）

| code | 含义 |
| --- | --- |
| `ORDER_LINE_ID_EMPTY` | 采购单行号为空 |
| `ORDER_LINE_DUPLICATE` | 采购单行号重复 |
| `ORDER_QUANTITY_INVALID` | 订购数量非正、格式非法或小数超过 3 位 |
| `ORDER_PRICE_INVALID` | 单价为负、格式非法或小数超过 4 位 |

### 核销错误码（逐张拒绝，见 `results[].error.code`）

| code | 含义 |
| --- | --- |
| `PO_LINE_NOT_FOUND` | `IT101` 行号不在采购单中 |
| `UNIT_PRICE_MISMATCH` | `IT104` 单价与采购单单价不符 |
| `INVOICE_DUPLICATE` | 发票号与已接受的发票重复 |
| `QUANTITY_EXCEEDED` | 累计数量超出订购数量 |
| （其余） | 原 `/v1/verify` 核验错误码，如 `NOT_ASCII`、`TDS_MISMATCH` 等 |

调用示例：

```bash
curl -s http://localhost:8080/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d '{
    "order_lines": [{"line_id": "1", "ordered_quantity": "10", "unit_price": "19.99"}],
    "edi_texts": ["ISA*00*          *00*          *ZZ*SENDER123456789*ZZ*RECEIVER1234567*000830*1200*U*00401*000000001*0*P*:~GS*IN*S*R*20200830*1200*42*X*004010~ST*810*0001~BIG*20200830*INV1~IT1*1*10*EA*19.99~TDS*19990~SE*5*0001~GE*1*42~IEA*1*000000001~"]
  }'
```

## 核验规则

报文结构（顺序固定，各 1 份，`IT1` 为 1～100 个）：

```
ISA → GS → ST → BIG → IT1(1..100) → TDS → SE → GE → IEA
```

- **分隔符**：ISA 段为固定 106 字节。字节偏移 3 取元素分隔符，偏移 104 取组件分隔符，
  偏移 105 取段终止符；三者必须互不相同且非字母数字。段终止符之后允许 CR/LF 排版空白。
- **版本**：`ISA12 == "00401"`、`GS08 == "004010"`、`ST01 == "810"`。
- **控制号配对**：`ISA13 == IEA02`、`GS06 == GE02`、`ST02 == SE02`。
- **计数**：`GE01 == 1`、`IEA01 == 1`；`SE01` 等于从 ST 到 SE（含两端）的段数。
- **业务字段**：`BIG02` 发票号非空；各 `IT101` 行号非空且全局唯一。
- **金额**：
  - `IT102` 数量为正，小数位至多 3 位；
  - `IT104` 单价非负，小数位至多 4 位；
  - 每行以整数定点数精确相乘（不受 Decimal 默认精度限制），按 `ROUND_HALF_UP` 取整到 0.01（整数分）；
  - 各行整数分之和必须等于 `TDS01`。`TDS01` 按 X12 `N2` 隐含两位小数解析
    （`"19990"` = 199.90），也接受显式小数（`"199.90"`，至多两位）。

### 错误码

| code | 含义 |
| --- | --- |
| `EMPTY_INPUT` | edi_text 为空 |
| `NOT_ASCII` | 含非 ASCII 字节 |
| `PAYLOAD_TOO_LARGE` | 超过 64 KiB |
| `ISA_NOT_FIRST` / `ISA_TOO_SHORT` / `ISA_MALFORMED` | ISA 不在开头 / 不足 106 字节 / 元素结构错误 |
| `INVALID_ELEMENT_SEPARATOR` / `INVALID_SEGMENT_TERMINATOR` / `INVALID_COMPONENT_SEPARATOR` / `SEPARATOR_CONFLICT` | 分隔符不合法或互相冲突 |
| `ISA12_BAD_VERSION` / `GS08_BAD_VERSION` / `ST01_NOT_810` | 版本或交易集类型不符 |
| `UNEXPECTED_SEGMENT` / `EMPTY_SEGMENT` / `MISSING_SEGMENT` / `TRAILING_DATA` | 段顺序错误、空段、未正常闭合、IEA 后多余数据 |
| `SEGMENT_TOO_FEW_ELEMENTS` | 段元素个数不足 |
| `BIG02_EMPTY` | 发票号为空 |
| `IT1_MISSING` / `IT1_TOO_MANY` / `IT101_EMPTY` / `IT101_DUPLICATE` | IT1 数量/行号问题 |
| `IT102_INVALID` / `IT104_INVALID` | 数量或单价格式/取值错误 |
| `TDS01_INVALID` / `TDS_MISMATCH` | TDS01 格式错误 / 与行合计不一致 |
| `SE01_INVALID` / `SE01_WRONG_COUNT` | SE01 格式错误 / 段数不符 |
| `GE01_INVALID` / `GE01_NOT_ONE` / `IEA01_INVALID` / `IEA01_NOT_ONE` | 计数字段错误 |
| `ISA_IEA_CONTROL_MISMATCH` / `GS_GE_CONTROL_MISMATCH` / `ST_SE_CONTROL_MISMATCH` | 控制号配对不一致 |

## 本地开发（不用 Docker）

Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

运行测试：

```bash
pip install -r requirements-dev.txt
pytest -q
```

## 仓库结构

```
app/main.py        FastAPI 路由、请求/响应模型、422 异常处理
app/verifier.py    ISA 分隔符解析、外壳状态机、整数定点金额核验
app/reconciler.py  采购单校验与多发票逐张核销（整数毫单位，无浮点）
tests/test_verify.py     /v1/verify 端到端/单元测试（39 个）
tests/test_reconcile.py  /v1/reconcile 端到端测试（25 个）
Dockerfile         python:3.12-slim 镜像，以 nobody 运行
docker-compose.yml 映射 8080 端口，含 /health 健康检查
```
