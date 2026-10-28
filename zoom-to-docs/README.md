# Zoom録画 → 議事録 → Googleドキュメント 自動作成

Zoomのクラウド録画に付く「音声文字起こし」を取得し、OpenAIで議事録の形に整えて、
Googleドキュメントとして指定のDriveフォルダに保存するスクリプトです。
毎朝4:00に前日の会議の議事録を作り、作成した件数とURLをGmailで報告します。

```
launchd(毎朝4:00) ─▶ Zoom API: 直近7日のクラウド録画を取得
                        └▶ 文字起こし(VTT)が完成している会議だけダウンロード
                              └▶ OpenAI: 概要・議題・決定事項・ToDo・課題に整理
                                    └▶ Googleドキュメントを作成(processed.json に記録)
                                          └▶ Gmailで件数とURLを報告
```

## 作成される議事録

| セクション | 内容 |
|---|---|
| タイトル・日時・参加者 | `議事録_2026-10-28_会議名`。参加者は文字起こしの発言者名 |
| 概要 | 会議の要点3〜5行 |
| 議題と内容 | 議題ごとの見出しと、話し合われた内容 |
| 決定事項 | 箇条書き |
| ToDo | チェックボックス付き。担当者・期限(不明なら「未定」) |
| 課題・次回までの確認事項 / 次回 | |
| 文字起こし(原文) | AIの読み違いを確認するため(`INCLUDE_TRANSCRIPT=false` で省略) |

- 文字起こしがまだ出来ていない会議はスキップし、次回の実行で再挑戦します
- 一度作った会議は `processed.json` に記録され、二重に作られません
  (直近7日分を見るので、Macが止まっていた日の会議も翌日以降に拾われます)
- 報告メールは**議事録を1件以上作った日**と**エラーで止まった日**だけ届きます(会議が無い日は届きません)
- 失敗した時と、Googleのログインが切れそうな時(ログインから6日後)は**LINEにも通知**します

---

## 1. Zoom側の準備

> **セットアップ済み**: 「4-3AIP連携課題」で作ったServer-to-Server OAuthアプリに、録画用のスコープを追加済みです。

1. Zoom Web の [録画の設定](https://zoom.us/profile/setting?tab=recording) で
   「**クラウド記録**」をオンにし、「**音声文字起こしを作成する**」にチェック(有料プランが必要)
2. [作成済みアプリ一覧](https://marketplace.zoom.us/user/build) → Server-to-Server OAuthアプリ →「Scopes」で次を追加
   - `cloud_recording:read:list_user_recordings:admin`(録画の一覧)
   - `cloud_recording:read:recording:admin`(録画ファイルの取得)
3. 会議では「レコーディング」→「**クラウドにレコーディング**」で録画する
   (日本語の会議は「字幕を表示」→「話す言語:日本語」を確認)

## 2. Google側の準備

> **セットアップ済み**: 「4-3AIP連携課題」の `client_secret.json` をコピー済みです。

1. Google Cloud Console で **Google Docs API**・**Google Drive API**・**Gmail API** を有効化
2. 「認証情報」→「OAuthクライアントID」→ 種類「**デスクトップアプリ**」で作成し、
   JSONをこのフォルダに **`client_secret.json`** として置く(`.gitignore`済み)
3. 保存先のDriveフォルダのURL `https://drive.google.com/drive/folders/【ここ】` を控える
4. 「OAuth同意画面」の公開ステータスが「**テスト**」のままだと、ログインが**約7日で切れます**。
   - **現在の運用(テスト中のまま)**: ログインから6日たつとLINEに予告が届くので、
     `python3 main.py --login` でログインし直す(切れてしまった場合も失敗通知がLINEに届く)
   - 「**アプリを公開**」(本番環境)にできれば、ログインし直す必要は無くなる
     (このプロジェクトでは「ブランディングページで構成を完了する必要があります」と出て公開できなかった)
   - Google Cloud Console →「Google Auth Platform」(旧「OAuth同意画面」)→「**対象**」→「**アプリを公開**」
   - 公開しても、アプリがどこかに掲載されたり、議事録やメールが他人に見えたりはしません
     (使えるのは `client_secret.json` を持っている人だけで、その人も**自分の**データしか触れない)
   - 審査は不要(個人利用)。ログイン時の「Googleで確認されていません」は「詳細」→「移動」で進める
   - 公開後に `python3 main.py --login` でログインし直す(テスト中に発行されたログインは7日で切れるため)

## 3. セットアップ

```bash
cd "/Users/katouayaka/Documents/5-1実戦課題/zoom-to-docs"

python3 -m venv venv           # 作成済みならスキップ
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env           # 作成済み(4-3AIP連携課題の値をコピー済み)
```

| 変数 | 必須 | 内容 |
|---|---|---|
| `ZOOM_ACCOUNT_ID` / `ZOOM_CLIENT_ID` / `ZOOM_CLIENT_SECRET` | ○ | 手順1のアプリの認証情報 |
| `OPENAI_API_KEY` | ○ | OpenAIのAPIキー |
| `OPENAI_MODEL` | | 議事録作成に使うモデル(デフォルト `gpt-5.4-mini`) |
| `GOOGLE_OAUTH_CLIENT_FILE` | | 手順2-2のJSON(デフォルト `client_secret.json`) |
| `GOOGLE_TOKEN_FILE` | | ログイン情報の保存先(デフォルト `token.json`) |
| `GOOGLE_DRIVE_FOLDER_ID` | | 保存先フォルダ。空ならマイドライブ直下 |
| `LOOKBACK_DAYS` | | 何日前までの録画を対象にするか(デフォルト 7) |
| `INCLUDE_TRANSCRIPT` | | `false` で文字起こしの原文を載せない |
| `MAIL_TO` | | 報告メールの宛先。空ならメールを送らない |
| `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_TO` | | 失敗・ログイン切れの予告をLINEに送る。`slack-to-line/.env` と同じ値 |

## 4. 動作確認(手動実行)

```bash
source venv/bin/activate

# ドキュメントを作らず、議事録の内容を画面で確認する
python3 main.py --dry-run

# 初回のみ: ブラウザが開くのでGoogleにログイン(token.json が保存される)
python3 main.py --login

# 本番実行
python3 main.py
```

`[INFO] 議事録を作成しました: ...` とURLが表示され、`MAIL_TO` に報告メールが届けば成功です。

## 5. 1日1回の定期実行(launchd)

`launchd/com.zoomtodocs.daily.plist` は、このフォルダのパスを設定済みです(毎朝4:00に実行)。
**先に手順4の `--login` でGoogleへのログインを済ませておいてください**(launchdからはブラウザを開けません)。

```bash
cp launchd/com.zoomtodocs.daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.zoomtodocs.daily.plist

# すぐ実行してログを確認
launchctl start com.zoomtodocs.daily
cat run.log launchd.err.log
```

停止する場合:
```bash
launchctl unload ~/Library/LaunchAgents/com.zoomtodocs.daily.plist
rm ~/Library/LaunchAgents/com.zoomtodocs.daily.plist
```

※ 4:00にMacがスリープ中だと、**次にMacを起こした時**に実行されます。シャットダウン中だとその日は実行されません
(直近7日分を見るので、翌日以降の実行で拾われます)。
4:00ちょうどに動かしたい場合は、3:55に自動でスリープ解除・起動する設定をしておきます(電源につないでおく):

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 03:55:00
pmset -g sched        # 設定の確認
sudo pmset repeat cancel   # 解除する場合
```

---

## トラブルシューティング

| 症状 | 原因・対処 |
|---|---|
| `Zoomトークン取得エラー: 400 ... invalid_client` | Client ID / Secret の誤り |
| `does not contain scopes: [cloud_recording:...]` | 手順1-2のスコープ不足。追加後はそのまま再実行でよい |
| `クラウド録画: 0件` | ローカル記録で録画している(手順1-3)、または7日より前の録画(`--days 30` で対象を広げる) |
| `文字起こし未完成のためスキップ` がずっと続く | 手順1-1の「音声文字起こしを作成する」がオフ。オフの間に録った会議には後から付かない |
| `Googleへのログインが必要です` | ログインが切れた。`python3 main.py --login` でログインし直す。頻繁に切れるなら手順2-4 |
| `invalid_grant` | 同上 |
| `必要な権限が足りないため…` | 権限(Gmail送信など)が増えた。`python3 main.py --login` でログインし直す |
| 報告メールが届かない | `MAIL_TO` が空、または会議が無かった日(作成0件の日は送らない)。迷惑メールフォルダも確認 |
| `File not found: (フォルダID)` | `GOOGLE_DRIVE_FOLDER_ID` の誤り、またはログイン中のGoogleアカウントのフォルダではない |
| 議事録の内容に聞き間違いが残る | Zoomの文字起こし自体の誤り。末尾の原文と見比べて修正する。`OPENAI_MODEL=gpt-5.5` などにすると改善することがある |
| 同じ会議の議事録を作り直したい | `processed.json` からその会議の行を消して再実行 |
| `NotOpenSSLWarning` などの警告 | macOS標準Pythonの警告で、動作には影響なし |

## ファイル構成

```
zoom-to-docs/
├── main.py                 # 本体スクリプト
├── requirements.txt        # 依存パッケージ
├── .env.example            # 環境変数テンプレート
├── .env                    # 実際の設定(.gitignore対象)
├── client_secret.json      # GoogleのOAuthクライアント(.gitignore対象)
├── token.json              # Googleのログイン情報(初回実行で作成・.gitignore対象)
├── processed.json          # 処理済みの会議(自動作成・.gitignore対象)
├── token_login_at.txt      # 最後にログインした日時(ログイン切れの予告用・.gitignore対象)
├── .gitignore
├── run.sh                  # launchd用の実行ラッパー
└── launchd/
    └── com.zoomtodocs.daily.plist
```
