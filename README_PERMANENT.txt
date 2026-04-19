永久保存版（Render + PostgreSQL + Persistent Disk）

檔案：
- boardgame_web_render_postgres.py
- requirements_postgres.txt
- render_postgres.yaml

做法：
1. 把這三個檔案放到 GitHub repo 根目錄。
2. 在 Render 選 New + Blueprint。
3. 連接你的 GitHub repo。
4. Render 會讀取 render_postgres.yaml，自動建立：
   - 一個 Web Service
   - 一個 PostgreSQL database
   - 一個 persistent disk（保留上傳圖片）
5. 部署完成後，用 Render 給你的網址開啟。

說明：
- DATABASE_URL：由 Render 自動注入到 Web Service。
- DATA_DIR=/var/data：遊戲圖片與成員圖片會寫到 persistent disk。
- 如果本機沒有 DATABASE_URL，程式會自動退回 SQLite，方便本地測試。

本機測試：
1. pip install -r requirements_postgres.txt
2. python boardgame_web_render_postgres.py
3. 打開 http://127.0.0.1:8000
