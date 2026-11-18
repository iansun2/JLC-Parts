# JLCPCB 離線元件搜尋器 (JLC-Parts Offline Search & CSV Database)

專為硬體工程師與 PCB 設計打造的 **JLCPCB 離線元件搜尋與 CSV 資料庫管理工具**。
預設針對 [MOSFETs (分類 2954)](https://jlcpcb.com/parts/2nd/Transistors_Thyristors/MOSFETs_2954) 建立完整 CSV 資料庫，並支援貼上任意 JLCPCB 分類網址或關鍵字擴充資料庫。

---

## ✨ 功能特色

1. **完整離線 CSV 資料庫 (`data/jlc_parts_db.csv`)**
   - 採用 `UTF-8 with BOM` 編碼，可直接用 **Excel** 開啟不亂碼（完整呈現 `Ω`、`℃`、`±` 等符號）。
   - 儲存完整 **`description`** 與拆解後的**電氣特性欄位**：
     - 通道類型 (`type_channel`: N-Channel / P-Channel 等)
     - 內部結構 (`config_number`: 1 N-channel / 2 N-Channel 等)
     - 漏源耐壓 (`vds` 與數值 `vds_num`)
     - 連續漏極電流 (`id_current` 與數值 `id_num`)
     - 導通電阻 (`rds_on` 與毫歐姆數值 `rdson_mohm`)
     - 閘極閾值電壓 (`vgs_th` 與數值 `vgsth_num`)
     - 最大閘源電壓 (`vgs_max`)、閘極電荷 (`qg`)、輸入/輸出/反向電容 (`ciss`, `coss`, `crss`)、最大功耗 (`power_pd`)、工作溫度 (`temp_range`)
     - 庫別 (`Basic` 基礎庫 / `Preferred` 優先庫 / `Extended` 擴充庫)、即時庫存 (`stock`)、價格階梯 (`price_1`, `price_10`, `price_100`, `price_tiers`)、Datasheet PDF 連結。

2. **高速本地網頁介面 (`app.py` + `static/index.html`)**
   - **多關鍵字即時檢索**：支援空白分隔多條件（如 `N-Channel SOT-23 30V`）與 `-排除詞`（如 `-TO-220`）。
   - **電氣參數範圍篩選**：可依 `Vds (V)`、`Id (A)`、`Rds(on) (mΩ)`、`Vgs(th) (V)`、`Pd (W)`、最低庫存與最高單價進行精準數值過濾。
   - **快速篩選預設標籤**：一鍵篩選「Basic/Preferred + N-Ch + SOT-23」、「低壓邏輯驅動 Vgs(th) ≤ 2.2V」、「超低內阻 Rds(on) ≤ 10mΩ」等。
   - **內建下載與擴充管理員**：直接在網頁上貼上任何 JLCPCB 分類網址或關鍵字，即可批次下載並合併至 CSV 資料庫。
   - **純離線檔案載入與匯出**：即使不啟動 Python 伺服器，直接用瀏覽器打開 `static/index.html` 也能透過「📂 載入本機 CSV」讀取任何 CSV 檔案；篩選後的結果也可一鍵匯出為新 CSV。

---

## 🚀 快速啟動方式

### 方式一：雙擊啟動（Windows）
直接雙擊執行目錄下的 [`start_search.bat`](./start_search.bat)，即會自動開啟瀏覽器進入 `http://127.0.0.1:8080`。

### 方式二：命令列啟動
```powershell
# 1. 啟動本地網頁伺服器 (預設 http://127.0.0.1:8080)
python app.py

# 2. 使用命令列下載或更新 CSV 資料庫
# 下載 MOSFETs 前 5,000 筆高庫存元件：
python jlc_fetcher.py --max 5000 --out data/jlc_parts_db.csv

# 下載 MOSFETs 全部有庫存元件 (無筆數上限)：
python jlc_fetcher.py --max 0 --out data/jlc_parts_db.csv

# 下載僅 Basic 基礎庫元件：
python jlc_fetcher.py --lib base --max 0 --out data/mosfets_basic.csv

# 貼上其他 JLCPCB 分類網址擴充至同一份 CSV：
python jlc_fetcher.py --url "https://jlcpcb.com/parts/2nd/Diodes/Schottky_Barrier_Diodes_SBD_2942" --max 3000 --out data/jlc_parts_db.csv
```

### 方式三：Docker Compose（Linux 伺服器）

資料庫 `data/jlc_parts.db`（約 560 MB）不包含在 Git 倉庫中，請先自行放入 `data/` 目錄，或用 `jlc_fetcher.py` 重新建立。`data/` 會掛載進容器，在網頁中新下載的元件也會保存在主機上。

```bash
# 建置並於背景啟動 (http://<主機 IP>:8385)
docker compose up -d --build

# 查看日誌
docker compose logs -f

# 停止並移除容器
docker compose down
```

- **連接埠**：預設 `0.0.0.0:8385` 對外開放。本工具**沒有登入驗證**，若只想本機使用，請將 `docker-compose.yml` 中的 `ports` 改為 `"127.0.0.1:8385:8080"`。
- **開機自動啟動**：容器設定 `restart: unless-stopped`，只要 Docker 服務本身開機自動啟動（`systemctl enable docker`），容器就會隨主機開機自動執行。若曾手動 `docker compose stop` / `down`，則需再執行一次 `docker compose up -d`。
