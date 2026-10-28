#!/usr/bin/env python3
"""
Zoomのクラウド録画に付く「音声文字起こし(VTT)」から、OpenAIで議事録を作成し、
Googleドキュメントとして指定のDriveフォルダに保存するスクリプト。

流れ:
1. Zoom API(Server-to-Server OAuth)で直近 LOOKBACK_DAYS 日分のクラウド録画を取得
2. 文字起こし(TRANSCRIPT)が完成している会議だけ、VTTをダウンロードして整形
3. OpenAIで「概要・議題ごとの内容・決定事項・ToDo・課題」に整理
4. Googleドキュメントを作成(見出し・箇条書き・チェックボックス付き)
5. 処理済みの会議を processed.json に記録し、次回以降は二重に作らない
6. MAIL_TO を設定すると、作成した件数とURL(または失敗の内容)をGmailで報告する
7. LINE_CHANNEL_ACCESS_TOKEN を設定すると、失敗した時とGoogleのログインが切れそうな時にLINEで知らせる
   (OAuth同意画面が「テスト中」だとログインが約7日で切れ、その間はGmailも送れないため)

文字起こしがまだ出来ていない会議はスキップし、次回の実行で再挑戦する。
1日1回などの定期実行(launchd / cron)を想定。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from openai import OpenAI

# ---------------------------------------------------------------------------
# .env 読み込み(依存ライブラリ不使用の簡易実装)
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def load_dotenv_if_present() -> None:
    """.env があれば読み込んで os.environ に設定する。"""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


# 設定を読み込む定数を参照する前に必ず.envを読み込んでおく
load_dotenv_if_present()

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

ZOOM_ACCOUNT_ID = os.environ.get("ZOOM_ACCOUNT_ID")
ZOOM_CLIENT_ID = os.environ.get("ZOOM_CLIENT_ID")
ZOOM_CLIENT_SECRET = os.environ.get("ZOOM_CLIENT_SECRET")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL") or "gpt-5.4-mini"

GOOGLE_OAUTH_CLIENT_FILE = os.environ.get("GOOGLE_OAUTH_CLIENT_FILE", "client_secret.json")
GOOGLE_TOKEN_FILE = os.environ.get("GOOGLE_TOKEN_FILE", "token.json")
# 任意: 空ならマイドライブ直下に保存
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")

# 任意: 作成完了の報告メールの宛先(空ならメールを送らない)
MAIL_TO = os.environ.get("MAIL_TO")

# 任意: 失敗・ログイン切れの予告をLINEに送る(slack-to-line と同じ値を使える)
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_TO = os.environ.get("LINE_TO")

# 何日前までの録画を対象にするか(Macがスリープしていた日の分も拾えるよう余裕を持たせる)
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS") or 7)
# 議事録の末尾に文字起こしの原文を載せるか(AIの読み違いを確認できるように)
INCLUDE_TRANSCRIPT = (os.environ.get("INCLUDE_TRANSCRIPT") or "true").lower() != "false"

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/gmail.send",
]
PROCESSED_FILE = BASE_DIR / "processed.json"
# 最後にブラウザでログインした日時(「テスト中」のアプリはログインから約7日で切れる)
LOGIN_AT_FILE = BASE_DIR / "token_login_at.txt"
LOGIN_WARN_DAYS = 6
LINE_TEXT_LIMIT = 4800
JST = ZoneInfo("Asia/Tokyo")

REQUIRED_ENV_VARS = {
    "ZOOM_ACCOUNT_ID": ZOOM_ACCOUNT_ID,
    "ZOOM_CLIENT_ID": ZOOM_CLIENT_ID,
    "ZOOM_CLIENT_SECRET": ZOOM_CLIENT_SECRET,
    "OPENAI_API_KEY": OPENAI_API_KEY,
}


def check_env() -> None:
    missing = [name for name, value in REQUIRED_ENV_VARS.items() if not value]
    if missing:
        print(f"[ERROR] 必須の環境変数が未設定です: {', '.join(missing)}", file=sys.stderr)
        print("        .env.example を参考に .env を作成してください。", file=sys.stderr)
        sys.exit(1)


def resolve_path(value: str) -> Path:
    """相対パスはこのフォルダ基準で解決する。"""
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path


# ---------------------------------------------------------------------------
# Zoom
# ---------------------------------------------------------------------------

def get_zoom_token() -> str:
    resp = requests.post(
        "https://zoom.us/oauth/token",
        params={"grant_type": "account_credentials", "account_id": ZOOM_ACCOUNT_ID},
        auth=(ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET),
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Zoomトークン取得エラー: {resp.status_code} {resp.text}")
    return resp.json()["access_token"]


def list_recordings(token: str, days: int) -> list[dict]:
    """直近 days 日分のクラウド録画(会議単位)を返す。"""
    today = datetime.now(timezone.utc).date()
    params = {
        "from": (today - timedelta(days=days)).isoformat(),
        "to": (today + timedelta(days=1)).isoformat(),
        "page_size": 300,
    }
    headers = {"Authorization": f"Bearer {token}"}
    meetings: list[dict] = []
    while True:
        resp = requests.get(
            "https://api.zoom.us/v2/users/me/recordings",
            headers=headers, params=params, timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Zoom録画一覧の取得エラー: {resp.status_code} {resp.text}")
        data = resp.json()
        meetings.extend(data.get("meetings", []))
        if not data.get("next_page_token"):
            return meetings
        params["next_page_token"] = data["next_page_token"]


def find_transcript_file(meeting: dict) -> dict | None:
    for f in meeting.get("recording_files", []):
        if f.get("file_type") == "TRANSCRIPT" and f.get("status") == "completed":
            return f
    return None


def download_transcript(token: str, file: dict) -> str:
    resp = requests.get(
        file["download_url"], headers={"Authorization": f"Bearer {token}"}, timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"文字起こしのダウンロードエラー: {resp.status_code} {resp.text[:200]}")
    return resp.content.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 文字起こしの整形
# ---------------------------------------------------------------------------

TIMESTAMP_LINE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.\d{3} --> ")
MERGE_MAX_CHARS = 200


def parse_vtt(vtt: str) -> list[tuple[str, str, str]]:
    """VTTを (開始時刻'mm:ss', 話者, 発言) のリストにする。
    同じ話者の連続発言は、読みやすい長さ(MERGE_MAX_CHARS)までまとめる。"""
    entries: list[tuple[str, str, str]] = []
    start = ""
    for line in vtt.splitlines():
        line = line.strip()
        match = TIMESTAMP_LINE.match(line)
        if match:
            h, m, s = (int(x) for x in match.groups())
            start = f"{h * 60 + m:02d}:{s:02d}"
            continue
        if not line or line == "WEBVTT" or line.isdigit():
            continue
        speaker, sep, text = line.partition(": ")
        if not sep:
            speaker, text = "(不明)", line
        if entries and entries[-1][1] == speaker and len(entries[-1][2]) < MERGE_MAX_CHARS:
            prev_start, _, prev_text = entries[-1]
            entries[-1] = (prev_start, speaker, prev_text + text)
        else:
            entries.append((start, speaker, text))
    return entries


def format_transcript(entries: list[tuple[str, str, str]]) -> str:
    return "\n".join(f"[{start}] {speaker}: {text}" for start, speaker, text in entries)


# ---------------------------------------------------------------------------
# OpenAI で議事録を作る
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """あなたは日本語の会議の議事録を作る担当者です。
Zoomの自動文字起こしを読み、議事録を作成してください。

ルール:
- 「えっと」「あの」などの言いよどみは除き、読みやすい文章に直す
- 文字起こしの明らかな聞き間違いは、文脈から正しい言葉に直す
- 文字起こしに無いことは書かない(推測で担当者や期限を埋めない)
- 担当者・期限が分からないToDoは "未定" とする
- 該当する内容が無い項目は空の配列にする

次のJSON形式だけを出力してください:
{
  "summary": ["会議の要点(3〜5行)"],
  "topics": [{"title": "議題", "points": ["話し合われた内容"]}],
  "decisions": ["決定事項"],
  "todos": [{"task": "やること", "owner": "担当者", "due": "期限"}],
  "open_issues": ["未解決の課題・次回までに確認すること"],
  "next_meeting": "次回の予定(無ければ空文字)"
}"""


def generate_minutes(client: OpenAI, topic: str, start: str, transcript: str) -> dict:
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"会議名: {topic}\n日時: {start}\n\n文字起こし:\n{transcript}"},
        ],
    )
    return json.loads(resp.choices[0].message.content)


# ---------------------------------------------------------------------------
# Google(ドキュメント・Gmail)
# ---------------------------------------------------------------------------

def get_google_credentials(force_login: bool = False) -> Credentials:
    token_path = resolve_path(GOOGLE_TOKEN_FILE)
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GOOGLE_SCOPES)
    if creds and not creds.has_scopes(GOOGLE_SCOPES):
        # 権限(スコープ)が後から増えた場合は、ログインし直して許可をもらう必要がある
        print("[WARN] 必要な権限が足りないため、Googleにログインし直します。")
        creds = None
    if creds and creds.valid and not force_login:
        return creds

    if creds and creds.expired and creds.refresh_token and not force_login:
        try:
            creds.refresh(Request())
        except RefreshError:
            # OAuth同意画面が「テスト」のままだと、約7日でログインが切れる
            print("[WARN] Googleのログインが期限切れです。ログインし直します。")
            creds = None

    if force_login or not creds or not creds.valid:
        if not force_login and not sys.stdin.isatty():
            raise RuntimeError(
                "Googleへのログインが必要です。`./venv/bin/python main.py --login` を"
                "実行し、ブラウザでログインしてください。"
            )
        # ブラウザでGoogleにログインする(launchd実行中は不可なので先に手動で済ませる)
        flow = InstalledAppFlow.from_client_secrets_file(
            str(resolve_path(GOOGLE_OAUTH_CLIENT_FILE)), GOOGLE_SCOPES
        )
        creds = flow.run_local_server(port=0)
        LOGIN_AT_FILE.write_text(datetime.now(JST).isoformat(timespec="seconds"), encoding="utf-8")

    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def utf16_len(text: str) -> int:
    """Docs APIの位置(index)はUTF-16単位で数える(絵文字などは2になる)。"""
    return len(text.encode("utf-16-le")) // 2


def build_doc_blocks(meta: dict, minutes: dict, transcript: str) -> list[tuple[str, str]]:
    """ドキュメントの中身を (段落の種類, テキスト) のリストにする。"""
    blocks: list[tuple[str, str]] = [
        ("TITLE", meta["title"]),
        ("NORMAL", f"日時: {meta['start']}(約{meta['duration']}分)"),
        ("NORMAL", f"参加者(発言者): {meta['speakers'] or '不明'}"),
    ]
    if meta.get("share_url"):
        blocks.append(("NORMAL", f"Zoom録画: {meta['share_url']}"))

    def section(heading: str, items: list[str], kind: str = "BULLET") -> None:
        blocks.append(("HEADING_2", heading))
        if items:
            blocks.extend((kind, item) for item in items)
        else:
            blocks.append(("NORMAL", "なし"))

    section("概要", minutes.get("summary", []))

    blocks.append(("HEADING_2", "議題と内容"))
    topics = minutes.get("topics", [])
    if not topics:
        blocks.append(("NORMAL", "なし"))
    for t in topics:
        blocks.append(("HEADING_3", t.get("title", "")))
        blocks.extend(("BULLET", p) for p in t.get("points", []))

    section("決定事項", minutes.get("decisions", []))
    section("ToDo", [
        f"{t.get('task', '')}(担当: {t.get('owner') or '未定'} / 期限: {t.get('due') or '未定'})"
        for t in minutes.get("todos", [])
    ], kind="CHECKBOX")
    section("課題・次回までの確認事項", minutes.get("open_issues", []))

    if minutes.get("next_meeting"):
        blocks.append(("HEADING_2", "次回"))
        blocks.append(("NORMAL", minutes["next_meeting"]))

    if INCLUDE_TRANSCRIPT:
        blocks.append(("HEADING_2", "文字起こし(原文)"))
        blocks.extend(("NORMAL", line) for line in transcript.splitlines())
    return blocks


def create_minutes_doc(creds: Credentials, title: str, blocks: list[tuple[str, str]]) -> str:
    drive = build("drive", "v3", credentials=creds)
    docs = build("docs", "v1", credentials=creds)

    body = {"name": title, "mimeType": "application/vnd.google-apps.document"}
    if GOOGLE_DRIVE_FOLDER_ID:
        body["parents"] = [GOOGLE_DRIVE_FOLDER_ID]
    doc_id = drive.files().create(body=body, fields="id").execute()["id"]

    # 全文を一度に挿入し、段落ごとに見出し・箇条書きのスタイルを付ける
    text = "".join(line.replace("\n", " ") + "\n" for _, line in blocks)
    requests_: list[dict] = [{"insertText": {"location": {"index": 1}, "text": text}}]
    index = 1
    for kind, line in blocks:
        end = index + utf16_len(line.replace("\n", " ") + "\n")
        rng = {"startIndex": index, "endIndex": end}
        if kind in ("TITLE", "HEADING_2", "HEADING_3"):
            requests_.append({
                "updateParagraphStyle": {
                    "range": rng,
                    "paragraphStyle": {"namedStyleType": kind},
                    "fields": "namedStyleType",
                }
            })
        elif kind in ("BULLET", "CHECKBOX"):
            preset = "BULLET_CHECKBOX" if kind == "CHECKBOX" else "BULLET_DISC_CIRCLE_SQUARE"
            requests_.append({"createParagraphBullets": {"range": rng, "bulletPreset": preset}})
        index = end

    docs.documents().batchUpdate(documentId=doc_id, body={"requests": requests_}).execute()
    return f"https://docs.google.com/document/d/{doc_id}/edit"


def send_mail(creds: Credentials, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["To"] = MAIL_TO
    msg["From"] = "me"
    msg["Subject"] = subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail = build("gmail", "v1", credentials=creds)
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()


def build_report(created: list[dict], skipped: list[str]) -> tuple[str, str]:
    today = datetime.now(JST)
    subject = f"【議事録】{len(created)}件作成しました({today:%m/%d})"
    lines = [f"Zoom録画から議事録を{len(created)}件作成しました。", ""]
    for item in created:
        lines += [f"■ {item['label']}", f"  {item['doc_url']}", ""]
    if skipped:
        lines += ["文字起こしが未完成のため、次回作成する会議:"]
        lines += [f"・{label}" for label in skipped]
        lines.append("")
    lines.append("※ AIが作成した議事録です。末尾の文字起こし(原文)と見比べて確認してください。")
    return subject, "\n".join(lines)


def send_to_line(text: str) -> None:
    if len(text) > LINE_TEXT_LIMIT:
        text = text[: LINE_TEXT_LIMIT - 20] + "\n…(以下省略)"

    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"messages": [{"type": "text", "text": text}]}

    if LINE_TO:
        url = "https://api.line.me/v2/bot/message/push"
        payload["to"] = LINE_TO
    else:
        url = "https://api.line.me/v2/bot/message/broadcast"

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"LINE送信エラー: {resp.status_code} {resp.text}")


def warn_if_login_expiring() -> None:
    """ログインから LOGIN_WARN_DAYS 日たったら、切れる前にLINEで予告する。"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LOGIN_AT_FILE.exists():
        return
    login_at = datetime.fromisoformat(LOGIN_AT_FILE.read_text(encoding="utf-8").strip())
    days = (datetime.now(JST) - login_at).days
    if days < LOGIN_WARN_DAYS:
        return
    send_to_line(
        f"【Zoom議事録】Googleのログインがまもなく切れます(前回ログインから{days}日)。\n"
        f"Macのターミナルで次を実行してログインし直してください:\n\n"
        f"cd \"{BASE_DIR}\" && ./venv/bin/python main.py --login"
    )
    print("[INFO] ログイン切れの予告をLINEに送りました")


def notify_failure(exc: Exception) -> None:
    """失敗したことをLINEとメールで知らせる(Googleのログイン切れだとメールは送れない)。"""
    if LINE_CHANNEL_ACCESS_TOKEN:
        try:
            send_to_line(
                f"【Zoom議事録】自動作成に失敗しました。\n\n{exc}\n\n"
                f"ログイン切れの場合は、Macのターミナルで次を実行してください:\n"
                f"cd \"{BASE_DIR}\" && ./venv/bin/python main.py --login"
            )
        except Exception as line_exc:  # noqa: BLE001
            print(f"[WARN] 失敗の通知をLINEに送れませんでした: {line_exc}", file=sys.stderr)
    if not MAIL_TO:
        return
    try:
        creds = get_google_credentials()
        send_mail(
            creds,
            "【議事録】自動作成に失敗しました",
            f"Zoom議事録の自動作成でエラーが発生しました。\n\n{exc}\n\n"
            f"詳しくは {BASE_DIR / 'run.log'} を確認してください。",
        )
    except Exception as mail_exc:  # noqa: BLE001
        print(f"[WARN] 失敗の報告メールも送れませんでした: {mail_exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 処理済みの記録
# ---------------------------------------------------------------------------

def load_processed() -> dict:
    if PROCESSED_FILE.exists():
        return json.loads(PROCESSED_FILE.read_text(encoding="utf-8"))
    return {}


def save_processed(processed: dict) -> None:
    PROCESSED_FILE.write_text(json.dumps(processed, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def to_jst(zoom_time: str) -> datetime:
    return datetime.fromisoformat(zoom_time.replace("Z", "+00:00")).astimezone(JST)


def main() -> None:
    parser = argparse.ArgumentParser(description="Zoom録画の文字起こしから議事録をGoogleドキュメントに作成")
    parser.add_argument("--dry-run", action="store_true",
                        help="ドキュメントを作らず、議事録の内容を画面に表示するだけ")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS,
                        help=f"何日前までの録画を対象にするか(デフォルト {LOOKBACK_DAYS})")
    parser.add_argument("--login", action="store_true",
                        help="ブラウザでGoogleにログインし直すだけ(ログインが切れた時に使う)")
    args = parser.parse_args()

    if args.login:
        get_google_credentials(force_login=True)
        print(f"[INFO] Googleにログインしました({GOOGLE_TOKEN_FILE} に保存)")
        return

    check_env()
    processed = load_processed()
    zoom_token = get_zoom_token()
    meetings = list_recordings(zoom_token, args.days)
    print(f"[INFO] 直近{args.days}日のクラウド録画: {len(meetings)}件")

    openai_client = OpenAI(api_key=OPENAI_API_KEY)
    creds = None if args.dry_run else get_google_credentials()
    created: list[dict] = []
    skipped: list[str] = []

    for meeting in sorted(meetings, key=lambda m: m.get("start_time", "")):
        uuid = meeting["uuid"]
        topic = meeting.get("topic", "(無題)")
        start = to_jst(meeting["start_time"])
        label = f"{start:%Y-%m-%d %H:%M} {topic}"

        if uuid in processed:
            continue
        transcript_file = find_transcript_file(meeting)
        if transcript_file is None:
            print(f"[INFO] 文字起こし未完成のためスキップ(次回再挑戦): {label}")
            skipped.append(label)
            continue

        entries = parse_vtt(download_transcript(zoom_token, transcript_file))
        if not entries:
            print(f"[WARN] 文字起こしが空のためスキップ: {label}")
            continue
        transcript = format_transcript(entries)
        speakers = "、".join(dict.fromkeys(s for _, s, _ in entries if s != "(不明)"))

        start_text = f"{start:%Y年%m月%d日 %H:%M}"
        minutes = generate_minutes(openai_client, topic, start_text, transcript)
        title = f"議事録_{start:%Y-%m-%d}_{topic}"
        blocks = build_doc_blocks(
            {
                "title": title,
                "start": start_text,
                "duration": meeting.get("duration", "?"),
                "speakers": speakers,
                "share_url": meeting.get("share_url"),
            },
            minutes,
            transcript,
        )

        if args.dry_run:
            print(f"\n===== {title} =====")
            for kind, line in blocks:
                prefix = {"HEADING_2": "\n## ", "HEADING_3": "### ", "BULLET": "・", "CHECKBOX": "□ "}.get(kind, "")
                print(prefix + line)
            continue

        url = create_minutes_doc(creds, title, blocks)
        processed[uuid] = {
            "topic": topic,
            "start_time": start.isoformat(),
            "doc_url": url,
            "created_at": datetime.now(JST).isoformat(timespec="seconds"),
        }
        save_processed(processed)  # 途中で失敗しても作成済みの分は二重に作らないよう毎回保存
        created.append({"label": label, "doc_url": url})
        print(f"[INFO] 議事録を作成しました: {label}\n       {url}")

    if args.dry_run:
        return
    print(f"[INFO] 完了(新規作成 {len(created)}件)")

    # 会議が無かった日はメールを送らない
    if created and MAIL_TO:
        send_mail(creds, *build_report(created, skipped))
        print(f"[INFO] {MAIL_TO} に報告メールを送りました")

    warn_if_login_expiring()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - 実行失敗はログに出して非ゼロ終了する
        print(f"[FATAL] {exc}", file=sys.stderr)
        notify_failure(exc)
        sys.exit(1)
