# quant：Binance 主网 / 测试网 API 连接基础

新增 [策略实验室使用说明](docs/strategy_lab.md)：最近市场数据、逐交易对比较三类策略、共用 50 USDT 预算的模型选币、测试网行情持续模拟及风险看板。入口为 `python -I scripts/strategy_lab.py watch`（首次先执行 `bootstrap` 和 `research`）。该工具只使用公开配置和公开行情，不加载账户密钥，也不发送交易所订单。

一个 Python 3.12 项目，默认连接 Binance **USDⓈ-M 合约测试网**（USDT 本位合约），验证网络连接和账户只读权限，为后续量化系统提供独立的连接层。另支持现货作为可选模式；暂不支持 COIN-M 币本位合约。

当前支持 HMAC API Key / Secret、测试网和主网、公开行情、账户认证、现货主网 API 权限查询。常规 `quant-binance` 客户端只允许固定的 GET 查询接口。新增的独立工具支持公开行情回放和用户启动的合约测试网单次开仓/平仓；不提供主网下单、调整杠杆、划转或提现。

| 模式 | 配置 | 官方 REST 地址 |
| --- | --- | --- |
| USDⓈ-M 合约测试网（默认） | `usdm` + `testnet` | `https://testnet.binancefuture.com` |
| USDⓈ-M 合约主网 | `usdm` + `mainnet` | `https://fapi.binance.com` |
| 现货测试网 | `spot` + `testnet` | `https://testnet.binance.vision` |
| 现货主网 | `spot` + `mainnet` | `https://api.binance.com` |

合约采用 `/fapi/v1/ping`、`/fapi/v1/time`、`/fapi/v2/ticker/price`、`/fapi/v3/account`。切换网络不会自动回退到主网。

本项目的 `usdm + testnet` 使用 [Binance 官方 Python SDK](https://github.com/binance/binance-connector-python/blob/master/common/src/binance_common/constants.py) 中的 `DERIVATIVES_TRADING_USDS_FUTURES_REST_API_TESTNET_URL`。该 SDK 将 `https://demo-fapi.binance.com` 单独列为 Demo 地址；本项目不会自动切换到该地址。公共接口连通不代表不同入口的密钥通用，账户鉴权需要单独验证。

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

请准备适用于 `https://testnet.binancefuture.com` 的合约测试网 HMAC API Key / Secret。Binance 提供 [Futures Demo Trading API 创建指引](https://www.binance.com/en/support/faq/detail/ab78f9a1b8824cf0a106b4229c76496d)，但该指引不能证明 Demo 凭据在本项目所选测试网入口也有效。已有凭据的兼容性请由你在自己的终端执行只读 `account` 命令验证；普通主网或现货测试网密钥不能替代目标环境的凭据。

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
BINANCE_API_KEY_MAIN=
BINANCE_API_SECRET_MAIN=
BINANCE_API_KEY_TEST=
BINANCE_API_SECRET_TEST=

# 以下为可选项；只配置上面四个变量也可以运行
BINANCE_MARKET=usdm
BINANCE_TIMEOUT_SECONDS=10
BINANCE_RECV_WINDOW_MS=5000
```

主网填写 MAIN 对应的两个变量，测试网填写 TEST 对应的两个变量。程序不再使用 `BINANCE_NETWORK` 或旧的无后缀密钥变量；即使本地文件仍有旧变量也会忽略。主网、测试网密钥不会互相回退。`.env` 仅由用户自己运行的认证程序读取，不提交到仓库；本次代码迁移没有读取或修改已有私有 `.env`。

同名系统环境变量优先于 `.env`。网络由 CLI 的 `--network`、终端交互选择或 Python 函数参数决定；不从 dotenv 决定。`Settings.load(network="mainnet")` 只选 MAIN 凭据，`Settings.load(network="testnet")` 只选 TEST 凭据。`market` 默认 `usdm`，仍可显式指定 `spot`；需要目标市场可用的密钥。

CLI 的公开 `ping` / `price` 不加载 `.env`；默认 USD-M 测试网，使用 `--network` / `--market` 切换。账户 CLI 从当前目录读取 `.env`，也可指定 `--env-file`，不向父目录搜索。双账户脚本默认从项目根目录读取 `.env`，可用 `--env-file` 覆盖。

## 使用

全局选项（`--market`、`--network`、`--env-file`、`--prompt-credentials`）放在子命令之前：

```powershell
# 公共接口，无需密钥
python -m quant_binance ping
python -m quant_binance price --symbol BTCUSDT

# 使用对应密钥验证账户，不显示资产和身份信息
python -m quant_binance --network mainnet account
python -m quant_binance --network testnet account

# 不传网络参数时，在交互终端选择 1=测试网、2=主网
python -m quant_binance account

# 可选现货公共连接，不使用账户凭据
python -m quant_binance --market spot --network testnet ping

# 仅限配置了对应现货主网凭据时使用；合约 Demo 权限请在官方 API 管理页查看
python -m quant_binance --market spot --network mainnet permissions

# 仅在自己的终端明确需要查看余额时使用；不要将输出上传或共享
python -m quant_binance --network testnet account --show-balances
```

也可将 `python -m quant_binance` 替换成 `quant-binance`。

账户验证成功的输出示例（示例数据，不代表已连接你的账户）：

```json
{
  "market": "usdm",
  "network": "testnet",
  "connection_mode": "system-route",
  "authenticated": true,
  "read_only_client": true,
  "balances_hidden": true,
  "positions_hidden": true
}
```

公共 `ping` 成功只说明网络连通；只有 `account` 成功才能证明密钥认证通过。`read_only_client` 描述本项目的接口限制，不表示密钥在 Binance 的实际权限只有读取。合约权限请查看官方 API 管理页；`permissions` 子命令只支持现货主网 SAPI。现货账户返回的 `canTrade` 不能代替 API Key 权限检查。

### 系统路由和 TUN

已删除硬编码代理及 `network_policy.py`，也不再使用 `--usdm-mainnet-proxy` 或 `--doh-proxy`。主网和测试网均使用普通网络连接，固定 `trust_env=False`，不继承 HTTP_PROXY / HTTPS_PROXY 等应用层代理。系统已开启的 TUN 仍按操作系统路由接管流量；程序不设置代理端口、不切换节点、不改 TUN、DNS 或路由。HTTPS 证书校验始终开启。

无需密钥的公开接口检查（默认检查两个网络的 Ping、服务器时间和 BTCUSDT 行情，共六项，全部通过才返回退出码 0）：

```powershell
python scripts/probe_binance_network.py
```

### 一次验证主网和测试网账户

由用户在自己的终端运行，默认按 MAIN / TEST 分别验证两个账户，不需要传网络参数：

```powershell
Set-Location C:\Users\jiangdaorui\Desktop\quant
conda activate quant
python scripts/verify_accounts.py
```

脚本先检查每个网络的公开接口，再使用对应密钥只读查询账户。输出 `public_connected` 和 `authenticated`，分别表示网络连通与签名认证是否通过；余额、持仓、账户身份、密钥、签名和原始错误内容不输出。普通认证失败后仍检查另一个网络；收到 418 / 429 时停止。两个账户均认证成功才返回退出码 0，不发送订单。

也可以只检查一个网络：

```powershell
python scripts/verify_accounts.py --network mainnet
python scripts/verify_accounts.py --network testnet
```

脚本调用方式（由用户运行，同样会加载本地凭据）：

```python
from quant_binance.verification import verify_account

print(verify_account("mainnet"))
print(verify_account("testnet"))
```

可在 `scripts/verify_accounts.py` 的 `NETWORKS` 元组中选择默认检查的网络。常规客户端也可以使用 `Settings.load(network="mainnet")` 或 `Settings.load(network="testnet")` 构造对应配置；不要打印 `client.account()` 返回的原始账户数据。

## 隐私与请求行为

- 密钥字段不出现在配置对象的 `repr` 中；CLI 不打印密钥、签名、请求 URL、服务器原始错误、账户 ID 或完整账户响应。
- 账户结果采用输出字段白名单，余额只有显式 `--show-balances` 才显示；合约持仓始终不输出。合约余额显示币种、钱包余额、可用余额、未实现盈亏，保留十进制精度。不会自动写入账户数据、日志或文件。
- HTTPS 证书校验保持开启，禁止重定向，只使用固定官方域名；不自动读取代理或自定义 CA 环境变量。
- 客户端固定 `trust_env=False`，所有市场使用系统路由及当前 TUN；失败不自动改用另一网络或另一组凭据。Codex 自身的网络设置不属于本项目。
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

测试使用 `httpx.MockTransport` 和内存中的虚构凭据；配置 I/O 被替换，不创建或读取 dotenv 文件，也不发起网络请求。覆盖签名、时间恢复、只读接口限制、禁止跳转、密钥不进入公共请求、错误脱敏、余额隐私和配置优先级。CI 仅做离线测试和扫描，不配置真实 Binance 密钥。

`scripts/check_secrets.py` 仅检查 Git 暂存区中的文件名和常见密钥格式，不读取本机 `.env`、其他私有配置或环境变量中的凭据。发现被禁止的文件名会直接阻止提交，连暂存区里的该文件内容也不会读取。它只报告文件名和规则，不报告匹配值。先暂存再扫描；如果没有暂存文件会失败。它不能发现所有形式的敏感数据。

## 不读取凭据的网络诊断

代理/TUN 网络排查使用独立脚本，它不会导入项目配置或加载 `.env`，仅访问 Binance 公共 GET 接口，不会修改代理、DNS、路由、网卡或防火墙：

```powershell
# 系统默认路由，不显式指定 HTTP 代理；开启 TUN 时仍可能经过代理
python scripts/probe_binance_network.py

# Windows：先查看物理网卡的 ifIndex，仅对测试 socket 设置出站接口
Get-NetAdapter | Select-Object Name,Status,ifIndex
python scripts/probe_binance_network.py --interface-index 16
```

`16` 仅为示例，实际网卡编号以本机输出为准。绑定在 TCP 连接建立之前通过 Windows `IP_UNICAST_IF` 设置，只影响当前测试连接。其他 VPN/WFP 层仍可能施加限制，因此不能把绑定接口本身当作完整的链路证明。脚本保留 TLS 证书验证，不跟随重定向，不打印公网出口 IP 或完整响应。

项目 `AGENTS.md` 禁止 AI 直接或间接访问私有 `.env`，也禁止 AI 运行会自动加载它的账户 CLI。账户认证请由用户在自己的终端执行。Codex 用户级 `config.toml` 可配置命名权限 profile 与文件 `deny`；已有 Full Access 任务不会因此自动切换为受限制的沙箱，旧版 CLI 也不能视为已获得保护。

## 行情采集与两种交易验证

### 公开行情与本地模拟成交

```powershell
conda activate quant
python scripts/testnet_workbench.py --symbol BTCUSDT --limit 500
```

该脚本不加载 `.env` 或账户凭据。它按系统路由从固定的合约测试网读取服务器时间、交易规则、1 分钟 K 线、买卖一价、标记价格和资金费率。仅保留已完成、连续且未过期的 K 线。JSON 结果保存到已被 Git 忽略的 `data/testnet/`，不上传到仓库。

本地回放采用 SMA 20/50 做多或空仓：使用前一根及更早的收盘价生成信号，在下一根开盘价上模拟成交。初始虚拟资金 10000 USDT，按报价计算的单次开仓名义金额不超过 200 USDT；包含每次 5 bps 假设手续费和 2 bps 假设滑点，未模拟资金费及强平。结果用于验证数据和策略流程，不代表测试网真实成交或策略盈利能力。`exchange_orders_sent` 始终为 0。

### 用户启动的账户验证及测试网单次往返交易

仅在你自己的终端运行。AI 不运行这个入口，因为它会加载你配置的 `.env`。

```powershell
conda activate quant
# 只验证账户；不发送订单
python -m quant_binance.testnet_execution

# 验证账户后，执行一次测试网开仓和平仓
python -m quant_binance.testnet_execution --round-trip --symbol BTCUSDT
```

执行器拒绝 `mainnet`、现货和其他目标域名。开始前必须没有任何合约持仓或挂单，并使用单向持仓模式；它不会改变账户模式或杠杆。执行期间不要同时运行其他交易程序。它更新测试网交易规则和买卖一价，按 `LOT_SIZE`、`MARKET_LOT_SIZE` 与 `MIN_NOTIONAL` 计算最小合法数量（保留 1% 名义金额余量），若按最新报价计算超过 200 USDT 或买卖价差超过 0.5%，则停止。该金额是下单前估算，市价实际成交仍受滑点影响。

往返流程只发送一次市价 BUY，再根据实际成交数量发送一次 `reduceOnly=true` 的市价 SELL。请求超时或 5xx 时，用唯一客户端订单号查询状态，不重新提交订单；429/418 立即停止。只有确认成交数量匹配、账户无剩余持仓才输出 `round_trip=completed` 和 `flat_after=true`。进程中断、未知订单状态或平仓失败时，需在官方测试网账户检查剩余持仓后再运行，程序不会声称已经平仓。

只验证账户时 `authenticated=true` 代表鉴权成功。往返模式没有该输出时，不能假设验证或交易成功。输出隐藏密钥、签名、账户身份、余额和订单详情，不保存账户响应。公开行情可用不证明当前 Key/Secret 适用于该测试网入口。

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

当前实现是公开行情回放和单次测试网接口验证。后续可添加持续采集、策略评估、故障恢复与审计；主网交易需要独立设计，本项目执行器不支持它。

## 官方参考

- [USDⓈ-M 合约行情与交易规则](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
- [USDⓈ-M 合约下单与订单查询](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade)
- [USDⓈ-M 合约：测试网地址、安全与签名](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info)
- [USDⓈ-M 合约账户 V3](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account)
- [USDⓈ-M 合约行情 V2](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
- [Spot REST 签名、安全和时间窗口](https://developers.binance.com/en/docs/products/spot/rest-api)
- [账户查询](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/account)
- [公共连接和服务器时间](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/general)
- [测试网 REST API](https://developers.binance.com/en/docs/products/spot/testnet/rest-api)
- [API Key 权限查询](https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/account)
