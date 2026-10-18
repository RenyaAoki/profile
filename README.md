# Personal Hub

プロフィール、外部リンク、現在の活動、プロジェクト、活動履歴を一か所で公開・更新するための小さなPersonal Hubです。温かいオフホワイトとセリフ体を組み合わせた、読みやすいエディトリアル調のデザインにしています。

## 動作環境

- Python 3.11以上
- Flask 3.x（`requirements.txt`）
- SQLite（Python標準ライブラリ）

この作業環境にはNode.jsがなかったため、Nuxtのビルド環境を追加せず、利用できるPython環境でサーバー描画のFlaskアプリとして実装しています。DBはSQLiteで、`.data/personal-hub.sqlite3` に保存します。`.data` はGit管理対象外です。

## 起動

PowerShellでこのフォルダーを開き、次を実行します。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:PERSONAL_HUB_ADMIN_USERNAME = "admin"
$env:PERSONAL_HUB_ADMIN_PASSWORD = "長くて推測されにくいパスワード"
python app.py
```

ブラウザーで `http://127.0.0.1:5000` を開きます。管理画面は `/admin` です。初回起動時に管理者パスワードのscryptハッシュをDBへ保存します。アプリのセッション署名鍵は未設定の場合 `.data/.session-secret` に生成されます。

## 管理画面

`/admin` でログイン後、Profile、SNS Links、Now、Projects、Activity、Thinking、Timelineを編集できます。公開・下書き切替、SNSリンクの表示切替、削除前の確認、並び順も管理できます。公開されていないProject、Activity、Thinking、Timelineは公開ページのHTMLに含まれません。

Profileの初期値は仕様書に記載されていた「Renya Aoki」「Student · Developer · Entrepreneur」「Technology × Business」を編集可能なスターターテキストとして入れています。本人情報に合わせて管理画面で更新してください。SNSや活動内容は創作せず、未登録なら公開ページに表示しません。

## 公開設定

デプロイ先では以下を設定してください。

- `PERSONAL_HUB_ADMIN_PASSWORD`: 初回DB作成時に使用する管理者パスワード
- `PERSONAL_HUB_SECRET_KEY`: 必要なら安定したランダムなセッション署名鍵（未設定の場合はデータディレクトリーに自動生成）
- `PERSONAL_HUB_PUBLIC_URL`: canonical URL用の公開URL（例 `https://example.com`）
- `PERSONAL_HUB_COOKIE_SECURE=true`: HTTPS環境でSecure Cookieを有効化
- `PERSONAL_HUB_DATA_DIR` / `PERSONAL_HUB_DATABASE`: SQLiteファイルの永続ボリュームを使う場合の保存先

SQLiteファイルが再起動後も残る永続ストレージへ置かれていることを、デプロイ先で確認してください。画像は初期版では公開HTTPS URLを指定します。ファイルアップロードと画像の永続ストレージ連携は未実装です。Flask開発サーバーはローカル確認用です。公開時は本番用WSGIサーバーとHTTPSのリバースプロキシを使用してください。

## 主なルート

- `/` — 公開プロフィールと活動一覧
- `/projects/<slug>` — 公開Projectの詳細
- `/admin` — 管理画面
- `/robots.txt` / `/sitemap.xml` — クロール用情報
