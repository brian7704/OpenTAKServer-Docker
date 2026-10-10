# DSM 註冊入口故障排除紀錄（2026-10-10）

本文件整理這次修正中實際遇到的問題、處理方法與驗證範圍。所有設定範例均使用假資料；不包含生產環境密碼、憑證或申請者資料。

## 問題與處理結果

| 現象 | 原因或判斷 | 處理方法 | 驗證結果 |
| --- | --- | --- | --- |
| 核准成功、設定包已產生，但寄信顯示 `'SMTP_HOST'` | 程式直接索引不存在的環境變數；此錯誤不是檔案權限錯誤 | 先檢查 SMTP_HOST；缺少或空白時回傳停用寄信狀態，保留核准與下載，顯示正確提示 | 缺少及空白 SMTP_HOST 的測試通過 |
| NAS 信箱需要 465 SSL，原寄信流程只支援 STARTTLS | 隱式 TLS 與 STARTTLS 的連線方式不同 | 新增 SMTP_SSL，採預設 SSL context 驗證伺服器憑證；兩模式不可同時啟用 | 本機 SSL/STARTTLS 測試及 NAS 465 TLS 登入通過 |
| 想沿用主 OpenTAKServer 郵件設定，但無法自動配置 | 主設定中的寄件者及登入帳號為空，不構成完整 SMTP 設定 | 停止匯入，改由管理者設定已啟用的 NAS MailPlus 信箱與既有密碼 | SMTP 登入、MAIL FROM、RCPT TO 通過 |
| 編輯 `.env` 後服務仍使用舊設定 | 環境變數在容器建立時載入，修改主機檔案不會更新執行中的程序 | 僅重新建立 register 服務；修改程式時另外重建映像 | 註冊容器重新建立，healthz 回傳 status=ok |
| 管理員能操作 DSM，但專案檔案權限仍需檢查 | DSM 帳號、主機 ACL、容器內程序權限及掛載權限是不同層次 | 檢查專案 ACL；管理員維護權限限於專案，含密碼的 `.env` 移除 Everyone 讀取權，保留必要管理及服務存取 | SMTP 驗證任務的 ACL 檢查通過 |
| 重新啟動後第一個 healthz 顯示 connection reset | 容器程序尚在啟動；單次失敗不足以判定服務故障 | 使用有限次重試，再要求一次成功健康回應 | 後續兩次回應 status=ok |
| GitHub 帳號為管理員，API 更新仍回傳 403 | 連接器回覆 Resource not accessible by integration；帳號管理權限不等於整合程式寫入權限 | 不修改 NAS 檔案權限來處理此錯誤；取得使用者同意後改用已登入的 GitHub 網頁提交 | 網頁可提交，逐批確認提交紀錄 |

## NAS MailPlus SMTP 設定範例

先在 MailPlus Server 確認帳號已啟用、信箱網域正確、SMTP SSL/TLS 465 已啟用，以及寄件地址屬於登入帳號。本次保留 SMTP 登入驗證與 TLS 憑證驗證。

```dotenv
SMTP_HOST=mail.example.com
SMTP_PORT=465
SMTP_SSL=true
SMTP_STARTTLS=false
SMTP_FROM=sender@example.com
SMTP_USERNAME=sender
SMTP_PASSWORD=''
```

密碼由管理者直接在 NAS 填寫。若改用 587，設定 SMTP_SSL=false、SMTP_STARTTLS=true。帳號與密碼必須成對設定，SMTP_PORT 必須介於 1–65535。不要把真實 `.env` 上傳 GitHub。

## 安全載入順序

1. 備份註冊程式與資料，記錄主 OpenTAKServer Compose 和設定檔的 SHA-256。
2. 更新註冊程式、原生 2FA 模組與範本；檢查語法及相關回歸測試。
3. 在 NAS 檢查 `.env` 的實際 ACL。Synology 顯示的 Unix 權限字串不足以單獨判斷 ACL；不要用全專案 chmod 777 解決密碼檔案存取問題。
4. 管理者直接填入既有信箱密碼，保存後關閉或最小化編輯器。
5. 從與註冊服務相同網路、環境設定執行 TLS 連線、SMTP AUTH、MAIL FROM 和 RCPT TO，然後 RSET。此步不呼叫 DATA，不寄出郵件。
6. 通過後僅重新建立註冊容器。從專案目錄執行：

   ```sh
   docker compose up -d --no-deps --force-recreate register
   ```

   若程式變更需重建映像，加上 `--build`。使用獨立 Compose 檔時加上 `-f compose.standalone.yml`，並確保位於該檔案的目錄。

7. 檢查註冊服務健康回應與主設定 SHA-256。保留既有 8443、8089、8446 及 ATAK 憑證設定，不刪除申請者資料。
8. 最後由管理者以正常申請流程確認實際通知與設定包附件送達。不要為測試重複核准既有申請或建立重複帳號。

## 原生管理員 2FA

管理審核頁使用 OpenTAKServer 原生登入及兩步驟驗證；需先在主站設定 2FA，並要求每次登入驗證。單純密碼登入不得建立管理工作階段，且必須確認 administrator 角色。

本次公開原生 2FA 模組、登入／驗證範本及回歸測試。測試覆蓋密碼登入拒絕、2FA、角色、CSRF、工作階段期限和登出；本機測試不能代替每個部署的真實 2FA 登入驗證。

## 已完成與尚待驗證

- 本機 SMTP 與管理員驗證共 12 個測試通過。
- NAS 註冊程式及容器內程式雜湊一致；服務健康檢查通過。
- NAS MailPlus 465 TLS、SMTP 登入及寄件／收件地址接受檢查通過。
- 主 OpenTAKServer Compose 與設定檔校驗一致。
- 本次 SMTP 驗證未寄出郵件。外部信箱實際送達、附件、垃圾郵件分類，以及 DNS／投遞政策仍須以實際郵件確認。
- 本文件不代表所有歷史登入、呼號、群組或連接埠問題均已逐項重新驗證。

## 遇到新錯誤時

先保留去識別化的例外類型、SMTP 回應碼與服務健康結果，不輸出密碼。認證失敗時確認帳號及服務要求；TLS 失敗時確認主機名稱與憑證；逾時時確認 DNS、路由及防火牆。不要關閉認證或憑證驗證來掩蓋錯誤。
