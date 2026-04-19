雲端部署說明（Render）

1. 把這個資料夾上傳到 GitHub。
2. 到 Render 建立新的 Web Service。
3. 選你的 GitHub repo。
4. Render 會自動讀 render.yaml。
5. 部署完成後，用 Render 給你的網址開啟。

注意：
- 這個版本使用 SQLite 資料庫檔案 boardgame_records.db。
- 如果你的雲端平台使用的是暫時磁碟，重新部署或重建服務後，資料可能會消失。
- 若你要長期保存資料，建議下一步改接 PostgreSQL。
