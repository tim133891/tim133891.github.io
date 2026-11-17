#!/usr/bin/env bash
set -Eeuo pipefail
die(){ echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "Run with sudo."
[[ -s /etc/openai-tunnel.env ]] || die "/etc/openai-tunnel.env missing; it holds your existing tunnel key and ID."
[[ -x /usr/local/bin/tunnel-client ]] || die "tunnel-client missing from /usr/local/bin."
LOGIN_USER="$SUDO_USER"
[[ -n "$LOGIN_USER" && "$LOGIN_USER" != "root" ]] || die "Run sudo from your regular admin account."
id "$LOGIN_USER" >/dev/null || die "User $LOGIN_USER not found."
command -v curl >/dev/null || die "curl not found."
command -v python3 >/dev/null || die "python3 not found."
echo "Preparing RHEL MCP for $LOGIN_USER..."
mkdir -p /opt/rhel-mcp
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
curl -fLSs --retry 3 --connect-timeout 15 --max-time 90 \
  "https://raw.githubusercontent.com/tim133891/tim133891.github.io/45cbe388ad08bf03196e198f11f08576fa850b84/rhel-mcp/server.py" -o "$tmp"
python3 -m py_compile "$tmp" || die "Syntax check failed; old tunnel untouched."
python3 - "$tmp" <<'PYTEST'
import json,subprocess,sys
requests=[
 {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1"}}},
 {"jsonrpc":"2.0","id":2,"method":"tools/list"},
 {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"run_command","arguments":{"command":"printf working"}}}
]
p=subprocess.run(["python3",sys.argv[1]],input="\n".join(json.dumps(v) for v in requests)+"\n",capture_output=True,text=True,timeout=15)
assert p.returncode==0,(p.returncode,p.stderr)
out=[json.loads(s) for s in p.stdout.splitlines()]
assert len(out)==3 and len(out[1]["result"]["tools"])>=25,(len(out),p.stderr)
assert "working" in out[2]["result"]["content"][0]["text"]
print("MCP self-test passed:",len(out[1]["result"]["tools"]),"tools")
PYTEST
install -o root -g root -m 755 "$tmp" /opt/rhel-mcp/server.py
unit=/etc/systemd/system/openai-tunnel.service
backup=/etc/systemd/system/openai-tunnel.service.pre-rhel-mcp
if [[ -f "$unit" && ! -f "$backup" ]]; then cp -a "$unit" "$backup"; fi
echo "Switching your existing tunnel from demo to real MCP..."
cat > "$unit" <<SERVICE
[Unit]
Description=OpenAI Secure MCP Tunnel - RHEL Management
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$LOGIN_USER
EnvironmentFile=/etc/openai-tunnel.env
Environment="MCP_COMMAND=/usr/bin/python3 /opt/rhel-mcp/server.py"
ExecStart=/usr/local/bin/tunnel-client run --health.listen-addr=127.0.0.1:0
Restart=always
RestartSec=5
UMask=0077

[Install]
WantedBy=multi-user.target
SERVICE
systemctl daemon-reload
systemctl enable openai-tunnel >/dev/null
systemctl restart openai-tunnel
sleep 2
if ! systemctl is-active --quiet openai-tunnel; then
  journalctl -u openai-tunnel -n 25 --no-pager
  die "Tunnel not active; previous unit saved at $backup"
fi
echo "SUCCESS: service active, real MCP installed."
echo "If ChatGPT still displays demo tools, refresh the Desktop Tunnel plugin."
