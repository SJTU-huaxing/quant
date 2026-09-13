# Private configuration boundary

- Never read, open, print, copy, hash, parse, load, upload, or otherwise access `.env` or private `.env.*` files. Do not inspect their contents through shell tools, Python, dotenv, Git, scanners, hooks, a child process, or another agent.
- A static `.env.example` template may be edited without consulting any private file.
- Never run `quant-binance` / `python -m quant_binance` from this project as an agent: its normal configuration loader opens `.env`. Account authentication must be performed by the user in their own terminal.
- For network diagnostics use `scripts/probe_binance_network.py`, which makes unauthenticated public requests and never loads project configuration, environment credentials, or dotenv files.
- Secret scans and Git hooks must inspect only Git index contents; do not load local secrets to compare against tracked files.
- Do not change, stop, restart, or reconfigure the user's proxy/VPN/TUN software, system proxy, DNS, firewall, or routing table. Only per-request proxy settings and per-socket interface binding are allowed for diagnostics.
- Do not claim that a file-deny policy is enforced by an already-running full-access session. Verify configuration without attempting to read protected files.
- Binance operations use direct connections with `trust_env=False` and no explicit proxy. Do not test through a proxy unless the user explicitly requests it again. This preference does not change Codex's own proxy setup.
- `scripts/testnet_workbench.py` may collect public testnet market data and simulate fills; it never loads dotenv or account credentials. Exchange orders are separate from these simulations.
- Never run `python -m quant_binance.testnet_execution` as an agent: it loads private configuration. The user must start account verification and testnet order execution in their own terminal.
