# quant：Binance 合约测试网 API 连接基础

一个 Python 3.12 项目，默认连接 Binance **USDⓈ-M 合约测试网 / Demo Trading**（USDT 本位合约），验证网络连接和账户只读权限，为后续量化系统提供独立的连接层。另支持现货作为可选模式；暂不支持 COIN-M 币本位合约。

当前支持 HMAC API Key / Secret、测试网和主网、公开行情、账户认证、现货主网 API 权限查询。只允许固定的 GET 查询接口，没有下单、撤单、调整杠杆、划转或提现实现。

| 模式 | 配置 | 官方 REST 地址 |
| --- | --- | --- |
| USDⓈ-M 合约测试网（默认） | `usdm` + `testnet` | `https://demo-fapi.binance.com` |
| USDⓈ-M 合约主网 | `usdm` + `mainnet` | `https://fapi.binance.com` |
| 现货测试网 | `spot` + `testnet` | `https://testnet.binance.vision` |
| 现货主网 | `spot` + `mainnet` | `https://api.binance.com` |

合约采用 `/fapi/v1/ping`、`/fapi/v1/time`、`/fapi/v2/ticker/price`、`/fapi/v3/account`。切换网络不会自动回退到主网。

## 安装

在项目根目录、能够使用 `conda` 的终端中运行：

```powershell
conda env create -f environment.yml
conda activate quant
python -m quant_binance ping
```

如果 `quant` 环境已经存在：

```powershell
conda activate quant
python -m pip install -e ".[dev]"
```

PowerShell 找不到 `conda` 时，可使用 Miniconda Prompt；也可用 Miniconda 安装目录下的 `Scripts/conda.exe run -n quant python ...`。无须修改全局 Python。

## 配置账户

在 [Binance Futures Demo Trading](https://demo.binance.com/) 登录/创建模拟账户，再进入该模拟账户的 API Management 创建 HMAC API Key / Secret，具体步骤见 [Binance 官方指引](https://www.binance.com/en/support/faq/detail/ab78f9a1b8824cf0a106b4229c76496d)。请使用合约 Demo 的凭据，普通主网或现货测试网的密钥不能替代它。

如果改用现货，可在 [Binance Spot Test Network](https://testnet.binance.vision/) 创建对应凭据。连接真实账户时使用独立凭据并配置 IP 白名单、最小权限；本阶段不需要下单、提现或转账。RSA / Ed25519 密钥目前不支持。

### 方式一：临时隐藏输入（不保存密钥）

在你自己的交互式终端执行，随后输入 Key 和 Secret，输入不会回显，也不进入命令历史：

```powershell
conda activate quant
python -m quant_binance --market usdm --network testnet --prompt-credentials account
# 使用主网凭据时改为 --network mainnet
```

不要在聊天、命令行参数、截图、GitHub Issues 或提交内容中粘贴密钥。

### 方式二：本地 .env

```powershell
# 仅在 .env 不存在时复制，避免覆盖已有配置
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

用本地编辑器填写 `.env`：

```dotenv
BINANCE_MARKET=usdm
BINANCE_NETWORK=testnet
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_TIMEOUT_SECONDS=10
BINANCE_RECV_WINDOW_MS=5000
```

填写等号后的合约 Demo Key / Secret，保留 `BINANCE_MARKET=usdm` 和 `BINANCE_NETWORK=testnet`。`.env` 是本机明文文件，不是加密保险库，请限制文件访问并避免同步或备份到共享位置。仓库只包含空白 `.env.example`。当前目录的 `.env` 会自动读取，也可用 `--env-file` 指定路径；不会向父目录搜索配置。

系统环境变量优先于 `.env`，显式 `--network` / `--market` 优先于两者。已配置凭据时，账户命令会拒绝用命令行将凭据切换到其他市场或网络；请更换匹配的 `.env` 文件，或使用隐藏输入提供目标网络凭据。长期自动运行建议由操作系统或部署平台的 Secret 管理器注入环境变量。不要将密钥写入 `environment.yml`、Conda 环境导出或 CI 配置。

## 使用

全局选项（`--market`、`--network`、`--env-file`、`--prompt-credentials`）放在子命令之前：

```powershell
# 公共接口，无需密钥
python -m quant_binance ping
python -m quant_binance price --symbol BTCUSDT

# 使用本地配置验证账户，不显示资产和身份信息
python -m quant_binance account

# 可选现货公共连接，不使用账户凭据
python -m quant_binance --market spot --network testnet ping

# 仅限配置了对应现货主网凭据时使用；合约 Demo 权限请在官方 API 管理页查看
python -m quant_binance --market spot --network mainnet permissions

# 仅在自己的终端明确需要查看余额时使用；不要将输出上传或共享
python -m quant_binance account --show-balances
```

也可将 `python -m quant_binance` 替换成 `quant-binance`。

账户验证成功的输出示例（示例数据，不代表已连接你的账户）：

```json
{
  "market": "usdm",
  "network": "testnet",
  "authenticated": true,
  "read_only_client": true,
  "balances_hidden": true,
  "positions_hidden": true
}
```

公共 `ping` 成功只说明网络连通；只有 `account` 成功才能证明密钥认证通过。`read_only_client` 描述本项目的接口限制，不表示密钥在 Binance 的实际权限只有读取。合约权限请查看官方 API 管理页；`permissions` 子命令只支持现货主网 SAPI。现货账户返回的 `canTrade` 不能代替 API Key 权限检查。

## 隐私与请求行为

- 密钥字段不出现在配置对象的 `repr` 中；CLI 不打印密钥、签名、请求 URL、服务器原始错误、账户 ID 或完整账户响应。
- 账户结果采用输出字段白名单，余额只有显式 `--show-balances` 才显示；合约持仓始终不输出。合约余额显示币种、钱包余额、可用余额、未实现盈亏，保留十进制精度。不会自动写入账户数据、日志或文件。
- HTTPS 证书校验保持开启，禁止重定向，只使用固定官方域名；不自动读取代理或自定义 CA 环境变量。需能够直接访问相应 Binance 服务的网络。
- 使用 HMAC-SHA256 签名、服务端时间同步和单调时钟；`recvWindow` 限定为 1–5000 ms。时间戳拒绝仅重新同步并重试一次只读查询。
- 所有请求有超时。429 / 418 立即停止，不循环重试，错误中显示有效的 `Retry-After` 秒数；这是连接基础，不是高频调度器。
- CLI 禁止 HTTP 库日志。直接调用 Python 客户端时，也不要开启 HTTP 调试日志、抓取带签名请求或记录原始响应；`account()` 返回的内存对象含私有数据。

`.gitignore`、提交前扫描和可选 Git hook 是防误提交措施，不能取代密钥管理。公开仓库一旦泄露密钥，应立即在 Binance 撤销并轮换，单纯删除文件不能撤销已泄露的凭据。

## 开发与验证

```powershell
python -m pytest
python -m ruff check .
python -m ruff format --check .
git add .
python scripts/check_secrets.py
# 可选：本仓库后续 commit 自动运行扫描
git config core.hooksPath .githooks
```

测试使用 `httpx.MockTransport` 和虚构凭据，不需要真实账户，也不发起网络请求。覆盖签名、时间恢复、只读接口限制、禁止跳转、密钥不进入公共请求、错误脱敏、余额隐私和配置优先级。CI 仅做离线测试和扫描，不配置真实 Binance 密钥。

`scripts/check_secrets.py` 检查整个 Git 暂存区中的文件名、常见密钥格式，以及本机已配置 Key / Secret 是否出现在待上传内容中。它只报告文件名和规则，不报告匹配值。先暂存再扫描；如果没有暂存文件会失败。

## 常见问题

| 结果 | 处理 |
| --- | --- |
| 缺少凭据 | 本地填写 Key / Secret，或用隐藏输入；不需要把凭据发给任何人 |
| `-2015` / `-2014` | 检查 HMAC 凭据、主网/测试网、读取权限、IP 白名单 |
| `-1022` | 检查 Secret 和密钥类型；不要把 RSA / Ed25519 私钥当 HMAC Secret |
| `-1021` | 已尝试一次时间同步恢复；检查延迟和系统时间 |
| HTTP 429 / 418 | 停止请求，按返回等待时间或 Binance 官方要求处理 |
| HTTP 403 / 451 | 检查所在网络的服务可用性和 Binance 访问要求 |
| 网络 / TLS / 超时 | 检查 DNS、直连网络、证书和防火墙；不要关闭 TLS 验证 |

## 后续扩展

当前范围是连接基础。后续可独立添加合约行情采集与回测层，再接入测试网执行层、保证金和杠杆限制、仓位模式、reduceOnly、订单幂等、交易规则校验、异常停机与审计，最后单独评审实盘执行。

## 官方参考

- [USDⓈ-M 合约：测试网地址、安全与签名](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info)
- [USDⓈ-M 合约账户 V3](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account)
- [USDⓈ-M 合约行情 V2](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
- [Spot REST 签名、安全和时间窗口](https://developers.binance.com/en/docs/products/spot/rest-api)
- [账户查询](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/account)
- [公共连接和服务器时间](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/general)
- [测试网 REST API](https://developers.binance.com/en/docs/products/spot/testnet/rest-api)
- [API Key 权限查询](https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/account)
