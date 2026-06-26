# Simple NMS — 簡易網路管理系統

繁體中文專案說明。English overview: [README.md](README.md).

## 專案概述

Simple NMS 是一套輕量級的網路管理系統，設計目標是用最少的元件完成網路事件的收集、儲存與即時呈現。整個系統只有一個 Python 進程，不需要外部 Web Server、Message Queue 或資料庫伺服器。

適用場景：中小型網路環境的 NOC（網路營運中心）值班監控、實驗室環境測試、或作為大型 NMS 的輕量補充。

### 核心能力

- **三種事件來源**：Syslog（UDP 514，支援 RFC 3164 與 RFC 5424）、SNMP Trap（UDP 162）、Webhook（HTTP POST）
- **統一儲存**：所有事件寫入單一 SQLite 資料庫，易於備份與查詢
- **即時儀表板**：瀏覽器開啟即可使用，支援即時事件、視覺化統計圖表、過濾、搜尋、排序、亮/暗主題
- **MIB 解析**：自動將 SNMP OID 翻譯為人類可讀的名稱（如 `IF-MIB::ifIndex`）
- **零外部依賴**：僅需 Python 3.9+ 與三個 pip 套件

---

## 系統架構

### 整體架構圖

```
                        ┌─────────────────────────────────────────┐
                        │            Simple NMS Process           │
                        │                                         │
  ┌──────────┐          │  ┌──────────────┐                       │
  │ 網路設備  │──UDP 514──▶│ Syslog       │                       │
  │ (Router,  │          │  │ Collector    │                       │
  │  Switch)  │──UDP 162──▶│ SNMP Trap    │──┐                    │
  └──────────┘          │  │ Collector    │  │                    │
                        │  └──────────────┘  │                    │
  ┌──────────┐          │                    │  ┌──────────────┐  │
  │ 外部服務  │──HTTP POST▶│ Webhook       │──┼─▶│ Write Queue  │  │
  │ (Grafana, │          │  │ Collector    │  │  │ (50K buffer) │  │
  │  CI/CD)   │          │  └──────────────┘  │  └──────┬───────┘  │
  └──────────┘          │                    │         │          │
                        │                    │  ┌──────▼───────┐  │
  ┌──────────┐          │                    │  │  DB Writer   │  │
  │ 瀏覽器   │◀─HTTP 80──│  Web Server       │  │  (批次寫入)   │  │
  │          │◀───SSE────│  (Flask)          │  └──────┬───────┘  │
  └──────────┘          │  ├─ GET /          │         │          │
                        │  ├─ GET /api/*     │         │ SSE 推送  │
                        │  ├─ GET /api/sse   │◀────────┘          │
                        │  └─ POST /webhook  │                    │
                        │                    │  ┌──────────────┐  │
                        │                    └─▶│   SQLite DB  │  │
                        │                       │  (WAL mode)  │  │
                        │                       └──────────────┘  │
                        └─────────────────────────────────────────┘
```

### 執行緒模型

整個系統運行在單一 Python 進程中，透過多執行緒處理並行任務：

| 執行緒 | 名稱 | 職責 |
|--------|------|------|
| Main Thread | `main` | 載入設定、啟動所有元件、處理 SIGINT/SIGTERM 信號 |
| Thread 1 | `syslog-collector` | 監聽 UDP 514，解析 RFC 3164 與 RFC 5424 PRI/欄位，推入 Write Queue |
| Thread 2 | `snmptrap-collector` | 監聽 UDP 162，透過 pysnmp 接收 Trap，MIB 解析後推入 Write Queue |
| Thread 3 | `web-server` | Werkzeug HTTP Server，處理 Web UI、REST API、Webhook、SSE |
| Thread 4 | `db-writer` | 從 Write Queue 取出事件，批次 INSERT 到 SQLite，成功後觸發 SSE 廣播 |

### 資料流

```
事件進入 → Collector 解析 → Queue.put() → DB Writer 批次 INSERT → SQLite
                                                    │
                                                    └→ SSE Hub 廣播 → 所有連線的瀏覽器
```

### 資料庫結構

單一表格 `events` 儲存所有類型的事件：

| 欄位 | 型別 | 說明 |
|------|------|------|
| `id` | INTEGER | 自動遞增主鍵 |
| `ts` | TEXT | UTC 時間戳（ISO 8601 格式） |
| `src_ip` | TEXT | 事件來源 IP |
| `type` | TEXT | 事件類型：`syslog` / `snmptrap` / `webhook` |
| `facility` | TEXT | Syslog facility（如 `local0`、`auth`） |
| `severity` | TEXT | 嚴重程度（如 `info`、`err`、`crit`） |
| `oid` | TEXT | SNMP Trap OID（MIB 解析後的名稱） |
| `varbinds` | TEXT | SNMP varbinds（JSON 格式） |
| `payload` | TEXT | 事件內容（Syslog 訊息 / Webhook JSON / Trap 摘要） |
| `tags` | TEXT | 自訂標籤 |

索引建立在 `ts`、`type`、`src_ip`、`severity` 四個欄位上，加速查詢與過濾。

資料庫啟用 WAL（Write-Ahead Logging）模式，允許讀寫並行，DB Writer 以每 100 筆或每 200ms 做一次批次寫入，實測吞吐量超過 5,000 events/sec。

---

## 檔案結構

```
simple-nms/
├── main.py                      # 入口：讀取設定、啟動所有元件
├── database.py                  # SQLite schema、DB Writer 執行緒
├── web_app.py                   # Flask app（Webhook + REST API + SSE + 靜態頁面）
├── config.json                  # 外部化設定（所有 port、MIB 路徑）
├── requirements.txt             # Python 依賴（flask, pysnmp, pysmi）
├── cleanup.py                   # 資料保留清除腳本
│
├── collectors/
│   ├── __init__.py
│   ├── syslog_listener.py       # Syslog UDP 收聽 + RFC 3164/5424 解析
│   ├── snmp_listener.py         # SNMP Trap 收聽 + MIB 解析 + Source IP 擷取
│
├── static/
│   └── index.html               # 完整 Web UI（HTML + CSS + JS，單一檔案）
│
├── deploy/
│   └── simple-nms.service       # systemd 服務單元檔
├── Dockerfile                   # Docker 容器化部署
├── docker-compose.yml           # Docker Compose 快速啟動
│
├── README.md                    # English project overview
├── README.zh-TW.md              # 繁體中文專案說明
├── INSTALL.md                   # 安裝指南
├── USER.md                      # 使用者手冊
│
├── test_phase1.py               # 測試：收集器 + DB Writer
├── test_phase2.py               # 測試：REST API + SSE
├── test_phase3.py               # 測試：Web UI 結構完整性
└── test_phase4.py               # 測試：可靠性 + 部署檔案
```

---

## 安裝教學

### 前提條件

- Debian 12 或 Ubuntu 22.04（其他 Linux 發行版亦可）
- Python 3.9+（Debian 12 預裝 3.11）
- pip3

### 第一步：安裝系統套件

```bash
sudo apt update
sudo apt install -y python3-pip snmp snmp-mibs-downloader
sudo download-mibs
```

### 第二步：安裝 Python 依賴

```bash
pip3 install flask werkzeug pysnmp pysmi --break-system-packages
```

| 套件 | 用途 |
|------|------|
| `flask` | Web Server（API + UI + SSE） |
| `werkzeug` | Flask 的 WSGI 底層 |
| `pysnmp` | SNMP Trap 接收與解析 |
| `pysmi` | ASN.1 MIB 自動編譯 |

### 第三步：部署檔案

```bash
cd ~
tar xzf simple-nms-final.tar.gz
cd simple-nms
```

### 第四步：設定 config.json

```json
{
    "database": {
        "path": "data/events.db",
        "wal_mode": true
    },
    "writer": {
        "batch_size": 100,
        "flush_interval_ms": 200
    },
    "syslog": {
        "enabled": true,
        "host": "0.0.0.0",
        "port": 514
    },
    "snmptrap": {
        "enabled": true,
        "host": "0.0.0.0",
        "port": 162,
        "community": "simplenms",
        "mib_dirs": ["/opt/simple-nms/data/mibs", "/usr/share/snmp/mibs"]
    },
    "webhook": {
        "enabled": true,
        "host": "0.0.0.0",
        "port": 80
    }
}
```

重要設定說明：

| 參數 | 說明 |
|------|------|
| `database.path` | SQLite 檔案位置（相對於執行目錄） |
| `writer.batch_size` | 每次批次寫入的最大筆數 |
| `writer.flush_interval_ms` | 未滿一批時的最長等待時間（毫秒） |
| `snmptrap.community` | SNMP community string（需與網路設備一致） |
| `snmptrap.mib_dirs` | ASN.1 MIB 檔案搜尋路徑（可放多個目錄） |
| `webhook.port` | Web Server 監聽的 HTTP port |

如果 Simple NMS 放在同一台主機的 HAProxy 後面，建議讓 Web Server 只監聽 loopback：

```json
"webhook": {
    "enabled": true,
    "host": "127.0.0.1",
    "port": 5000
}
```

HAProxy 需送出 forwarding header：

```haproxy
frontend http_in
    bind *:80
    mode http
    default_backend simple_nms

backend simple_nms
    mode http
    option forwardfor
    http-request set-header X-Forwarded-Proto http
    server simple_nms_1 127.0.0.1:5000 check
```

Webhook 的 `src_ip` 在本機 HAProxy 轉送時會使用 `X-Forwarded-For` 的第一個有效 IP；若沒有 proxy，則使用直接連線的來源 IP。Simple NMS 只在 immediate peer 是 loopback 時信任 `X-Forwarded-For` / `X-Real-IP`，避免外部直接連線偽造來源 IP。

### 第五步：手動測試啟動

```bash
sudo python3 main.py
```

瀏覽器打開 `http://your-server-ip` 即可看到儀表板。

### 第六步：安裝為 systemd 服務

```bash
# 建立專用使用者
sudo useradd -r -m -d /opt/simple-nms -s /usr/sbin/nologin simplenms

# 部署檔案
sudo cp -r ~/simple-nms/* /opt/simple-nms/
sudo chown -R simplenms:simplenms /opt/simple-nms

# 安裝服務
sudo cp /opt/simple-nms/deploy/simple-nms.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now simple-nms

# 確認狀態
sudo systemctl status simple-nms
sudo journalctl -u simple-nms -f
```

systemd 服務使用 `AmbientCapabilities=CAP_NET_BIND_SERVICE`，不需要 root 權限就能綁定 port 80/514/162。

---

## 使用教學

### 送入測試事件

#### Syslog

```bash
# 使用 logger 指令
logger -n 127.0.0.1 -P 514 --udp -p local0.err "BGP peer 10.0.0.1 down"
logger -n 127.0.0.1 -P 514 --udp -p auth.warning "Failed SSH login from 203.0.113.42"

# 使用 Python
python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.sendto(b'<134>router01 BGP: peer established', ('127.0.0.1', 514))
s.close()
"
```

#### SNMP Trap

```bash
# linkDown trap
snmptrap -v2c -c simplenms 127.0.0.1:162 '' \
  1.3.6.1.6.3.1.1.5.3 \
  1.3.6.1.2.1.2.2.1.1 i 3 \
  1.3.6.1.2.1.2.2.1.2 s "GigabitEthernet0/1"

# 使用 MIB 名稱（需要 snmp-mibs-downloader）
snmptrap -v2c -c simplenms 127.0.0.1:162 '' \
  IF-MIB::linkDown \
  IF-MIB::ifIndex i 3 \
  IF-MIB::ifDescr s "GigabitEthernet0/1"
```

#### Webhook

```bash
curl -X POST http://localhost/webhook \
  -H "Content-Type: application/json" \
  -d '{"event":"deploy","service":"web-api","severity":"info","message":"v2.3.1 deployed"}'
```

### Web UI 操作

#### KPI 卡片

頁面頂部四張卡片即時顯示事件總數與各類型計數，透過 SSE 自動更新。

#### 事件表格

點擊 Message 欄位可展開 Modal 視窗查看完整內容（JSON 會自動 pretty-print），支援一鍵複製。

#### 欄位排序

點擊任一欄位標頭可切換升序/降序，排序偏好自動存入 `localStorage`。

#### 過濾

- **全局搜尋**：頁首搜尋框，跨 payload、src_ip、facility、severity、oid、tags 模糊搜尋
- **時間範圍**：側邊欄提供 5 分鐘 / 1 小時 / 今天 / 全部 快捷按鈕，或自訂日期範圍
- **事件類型**：勾選框過濾 Syslog / SNMP Trap / Webhook
- **來源 IP**：文字輸入框，支援前綴匹配（如輸入 `10.0.0` 可匹配所有 10.0.0.x）

右上角 ☀/☽ 按鈕切換亮色/暗色主題，偏好自動記憶。

#### 視圖切換 (頁籤)

在事件總覽和數據分析頁籤之間切換：
- **Live Feed (即時事件)**：顯示過濾後的即時事件列表。
- **Analytics (數據分析)**：以 Chart.js 渲染視覺化圖表，包含：事件趨勢時間軸、事件類型佔比、嚴重程度分佈與 Top 10 來源 IP，圖表會隨側邊欄過濾條件即時連動。

### REST API

#### 查詢事件

```bash
# 最近 50 筆（預設）
curl "http://localhost/api/events"

# Syslog 錯誤事件，按時間降序
curl "http://localhost/api/events?type=syslog&severity=err,crit&sort=ts&order=desc"

# 全文搜尋 BGP 相關
curl "http://localhost/api/events?q=BGP"

# 特定 IP + 時間範圍
curl "http://localhost/api/events?src_ip=10.0.0&time_from=2026-04-14T08:00:00"

# 分頁：第 2 頁，每頁 20 筆
curl "http://localhost/api/events?page=2&per_page=20"
```

#### KPI 計數

```bash
curl "http://localhost/api/kpi"
# 回傳：{"total": 1685, "syslog": 1284, "snmptrap": 312, "webhook": 89}
```

#### 統計數據分析

```bash
curl "http://localhost/api/analytics"
# 回傳包含 types、severities、top_ips、timeline 與 timeline_scale 的聚合 JSON 數據
```

#### SSE 即時串流

```javascript
const es = new EventSource('/api/sse');
es.onmessage = (e) => {
    const event = JSON.parse(e.data);
    console.log('New event:', event);
};
```

#### 健康檢查

```bash
curl "http://localhost/health"
# 回傳：{"status": "healthy", "sse_clients": 2}
```

---

## MIB 解析

### 運作原理

Simple NMS 使用 pysnmp 的內建預編譯 MIB 進行 OID 的解析，後續收到 SNMP Trap 時，透過 `MibResolver` 將數字 OID 翻譯為可讀名稱。

解析結果會被快取（cache），同一個 OID 不會重複查詢。無法解析的 OID 會保留原始數字格式，不影響系統運作。

### 設定方式

在 `config.json` 的 `snmptrap` 區塊設定：

```json
"mib_dirs": ["/opt/simple-nms/data/mibs", "/usr/share/snmp/mibs"]
```

- `mib_dirs`：ASN.1 MIB 原始檔所在目錄，可指定多個

### 加入自訂 MIB

把廠商提供的 MIB 檔案放到 `mib_dirs` 指定的目錄即可（例如 `/opt/simple-nms/data/mibs`），系統啟動時會自動偵測並載入。

重啟 Simple NMS 即可。

---

## 可靠性機制

### 寫入失敗處理

DB Writer 在 SQLite 寫入失敗時，會立即記錄錯誤日誌並增加丟棄事件的指標計數（dropped metrics），以避免阻塞寫入佇列（Write Queue）導致新事件無法接收。

### SSE 斷線重連

瀏覽器的 EventSource 在連線中斷時會自動重連。同時前端有 30 秒 fallback polling 作為備援。SSE Hub 使用 per-client queue，每 15 秒發送 keepalive 防止 proxy/NAT 逾時斷線。

### 資料保留

使用 `cleanup.py` 定期清除舊事件：

```bash
# 預覽（不刪除）
python3 cleanup.py --days 30 --dry-run

# 執行清除
python3 cleanup.py --days 30

# 搭配 cron 每天凌晨 3 點自動清除
0 3 * * * cd /opt/simple-nms && python3 cleanup.py --days 30
```

---

## 網路設備設定範例

### Syslog 轉發

Cisco IOS：

```
logging host 10.0.0.100 transport udp port 514
logging trap informational
```

Juniper Junos：

```
set system syslog host 10.0.0.100 any info
set system syslog host 10.0.0.100 port 514
```

Linux rsyslog：

```
# /etc/rsyslog.d/50-nms.conf
*.* @10.0.0.100:514
```

### SNMP Trap 目的地

Cisco IOS：

```
snmp-server host 10.0.0.100 version 2c simplenms
snmp-server enable traps
```

Juniper Junos：

```
set snmp trap-group nms-traps targets 10.0.0.100
set snmp trap-group nms-traps version v2
```

### Webhook 整合

任何支援 outgoing webhook 的系統都可以 POST JSON 到 `http://your-server/webhook`，例如 Grafana Alertmanager、Zabbix、自訂腳本等。

若透過本機 HAProxy 對外服務，請在 backend 啟用 `option forwardfor`，Simple NMS 會在安全條件下記錄原始 client IP。

---

## 測試

專案包含 193 項自動化測試，涵蓋四個階段：

```bash
python3 test_phase1.py    # 43 項：Syslog 解析、Webhook 驗證、DB Writer、效能基準
python3 test_phase2.py    # 45 項：REST API 過濾/分頁/排序、SSE Hub、端對端整合
python3 test_phase3.py    # 57 項：Web UI HTML 結構、CSS 功能、JS 邏輯
python3 test_phase4.py    # 依目前測試更新：重試機制、清除腳本、部署檔案、文件完整性
```

效能基準測試結果：DB Writer 批次寫入達 **5,600+ events/sec**，遠超 RFP 要求的 500 events/sec。
