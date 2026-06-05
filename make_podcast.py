import os
import sys
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import requests
import feedparser
from pydub import AudioSegment

# === 設定項目 ===
# ニュースソース（RSSフィード）
RSS_FEEDS = [
    "https://www.publickey1.jp/atom.xml",
    "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml",
    "https://gihyo.jp/feed/atom",
    "https://b.hatena.ne.jp/hotentry/it.rss"
]

# VOICEVOX設定
VOICEVOX_URL = "http://localhost:50021"
SPEAKER_ZUNDAMON = 3  # ずんだもん（ノーマル）
SPEAKER_METAN = 2     # 四国めたん（ノーマル）

# 出力ファイルパス
HISTORY_FILE = "podcast_history.json"
CARRYOVER_FILE = "carryover.json"
PRIORITIES_FILE = "priorities.json"
PRONUNCIATION_FILE = "pronunciation.json"
OUTPUT_DIR = "podcasts"
RSS_FILE = "podcast.xml"

# === 共通ユーティリティ ===
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading {path}: {e}")
    return default

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving {path}: {e}")

# === 1. ニュース収集・フィルタリング ===
def fetch_news():
    print("Fetching news feeds...")
    articles = []
    
    # 優先度ルールの読み込み
    priorities = load_json(PRIORITIES_FILE, {"must_include": [], "boost": []})
    must_include = priorities.get("must_include", [])
    boost_words = priorities.get("boost", [])

    # 重複排除履歴（過去7日間）の読み込み
    history = load_json(HISTORY_FILE, [])
    # 7日以上前の履歴はクリーンアップ
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    history = [h for h in history if h.get("fetched_at", "") > cutoff_date]
    save_json(HISTORY_FILE, history)
    
    used_urls = {h["url"] for h in history}

    # RSSフィードの読み込み
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                title = entry.get("title", "")
                link = entry.get("link", "")
                summary = entry.get("summary", entry.get("description", ""))
                
                if not title or not link:
                    continue
                if link in used_urls:
                    continue
                
                # スコアリング（優先度判定）
                score = 0
                text_to_check = (title + " " + summary).lower()
                
                # must_includeワードが含まれる場合スコアを大幅に加算
                for word in must_include:
                    if word.lower() in text_to_check:
                        score += 100
                
                # boostワードが含まれる場合スコアを加算
                for word in boost_words:
                    if word.lower() in text_to_check:
                        score += 10
                
                articles.append({
                    "title": title,
                    "url": link,
                    "summary": summary,
                    "score": score,
                    "fetched_at": datetime.now(timezone.utc).isoformat()
                })
        except Exception as e:
            print(f"Error fetching feed {url}: {e}")

    # 前日の未採用記事（繰り越し分）を読み込んで結合
    carryover_articles = load_json(CARRYOVER_FILE, [])
    print(f"Loaded {len(carryover_articles)} carryover articles.")
    # 繰り越し記事も重複排除
    for art in carryover_articles:
        if art["url"] not in used_urls:
            # 繰り越し分は当日優先のためスコアを少し下げるが、優先キーワードは引き継ぐ
            art["score"] = max(0, art["score"] - 5)
            articles.append(art)

    # 重複排除（同じURLが複数ソースにある場合）
    unique_articles = {}
    for art in articles:
        if art["url"] not in unique_articles or art["score"] > unique_articles[art["url"]]["score"]:
            unique_articles[art["url"]] = art

    sorted_articles = sorted(unique_articles.values(), key=lambda x: x["score"], reverse=True)
    return sorted_articles

# === 2. Gemini API を使用した台本生成 ===
def generate_script_with_gemini(articles_batch, is_first, is_last):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY environment variable is not set.")
        sys.exit(1)

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
    
    # 記事テキストの作成
    articles_text = ""
    for i, art in enumerate(articles_batch):
        # HTMLタグの簡易除去
        clean_summary = re.sub(r'<[^>]*>', '', art['summary'])
        articles_text += f"【記事{i+1}】\nタイトル: {art['title']}\n概要: {clean_summary[:300]}\n\n"

    # プロンプトの設計
    role_instruction = (
        "あなたはテック系ポッドキャストの優秀な台本作家です。\n"
        "以下のニュース記事をもとに、解説役の「ずんだもん」と聞き手役の「四国めたん」の掛け合いによる日本語のラジオ台本を作成してください。\n"
        "【キャラクター設定】\n"
        "- ずんだもん（話者ID: 1）: テック事情に詳しくて解説をする役。語尾に「〜なのだ」「〜のだ」を多用する、少し元気で子供っぽい口調。\n"
        "- 四国めたん（話者ID: 2）: 聞き手役。ずんだもんの解説に質問したり、相槌を打つ丁寧で知的な女性。語尾は「〜ね」「〜ですよ」などお嬢様風の上品な口調。\n"
        "\n"
        "【出力フォーマット】\n"
        "必ず以下の形式のみで出力してください。余計な解説文や挨拶、Markdownの装飾（**など）は絶対に含めないでください。\n"
        "1: (ずんだもんのセリフ)\n"
        "2: (めたんのセリフ)\n"
        "1: (ずんだもんのセリフ)\n"
        "※行の先頭は必ず「1: 」か「2: 」で始めてください。半角英数のコロンとスペースを空けてください。\n"
    )

    if is_first and is_last:
        flow_instruction = (
            "番組全体の台本を作成します。冒頭に「おはようございます」などの短いオープニング挨拶を入れ、"
            "ニュースを紹介し、最後は「それではまた明日！」というエンディングトークで締めくくってください。"
        )
    elif is_first:
        flow_instruction = (
            "ポッドキャストの「第1パート」の台本です。冒頭に「おはようございます」などのオープニング挨拶を入れ、"
            "ニュースの解説を行ってください。挨拶が終わったらすぐに本題に入ってください。"
            "バッチ処理で後続のニュースが続くため、全体の締めくくりや「それではまた明日」といった終わりの挨拶は絶対に含めないでください。"
        )
    elif is_last:
        flow_instruction = (
            "ポッドキャストの「最終パート」の台本です。冒頭の挨拶は絶対に含めず、すぐにニュースの解説から始めてください。"
            "ニュースの解説が終わったら、最後に「今日も良い一日を！」「それではまた！」といったエンディングトークで締めくくってください。"
        )
    else:
        flow_instruction = (
            "ポッドキャストの「中間パート」の台本です。冒頭の挨拶（おはようございます等）も、"
            "最後の締めくくり（それではまた明日等）も絶対に含めないでください。純粋にニュース記事の紹介・解説の掛け合いだけを作成してください。"
        )

    prompt = f"{role_instruction}\n【今回の流れに関する指示】\n{flow_instruction}\n\n【紹介する記事の情報】\n{articles_text}"

    payload = {
        "contents": [{
            "parts": [{
                "text": prompt
            }]
        }],
        "generationConfig": {
            "temperature": 0.7
        }
    }
    
    headers = {
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        res_json = response.json()
        script_text = res_json['candidates'][0]['content']['parts'][0]['text']
        return script_text
    except Exception as e:
        print(f"Gemini API call failed: {e}")
        return ""

def process_and_combine_scripts(articles):
    # 最大6本のニュースを取り上げる
    target_articles = articles[:6]
    carryover_articles = articles[6:20] # 残りは翌日に繰り越す
    save_json(CARRYOVER_FILE, carryover_articles)
    print(f"Selected {len(target_articles)} articles for today. Carried over {len(carryover_articles)} articles.")

    # 3記事ずつのバッチに分割
    batch_size = 3
    batches = [target_articles[i:i + batch_size] for i in range(0, len(target_articles), batch_size)]

    combined_script = []
    
    for idx, batch in enumerate(batches):
        is_first = (idx == 0)
        is_last = (idx == len(batches) - 1)
        print(f"Generating script for batch {idx+1}/{len(batches)}...")
        
        script = generate_script_with_gemini(batch, is_first, is_last)
        if script:
            # 行ごとに分割してパース
            for line in script.split("\n"):
                line = line.strip()
                if line.startswith("1:") or line.startswith("2:"):
                    combined_script.append(line)
        time.sleep(2)  # API制限対策のウェイト
        
    return combined_script, target_articles

# === 3. カタカナ読み替え (発音修正) ===
def apply_pronunciation_fixes(script_lines):
    pronunciation_dict = load_json(PRONUNCIATION_FILE, {})
    fixed_lines = []
    
    # 辞書のキーを長さ順（長い順）にソートして、長い単語から優先して置換する（部分一致の誤置換防止）
    sorted_keys = sorted(pronunciation_dict.keys(), key=len, reverse=True)
    
    for line in script_lines:
        match = re.match(r"^([12]):\s*(.*)$", line)
        if not match:
            continue
        speaker_id = match.group(1)
        text = match.group(2)
        
        # 読み替え辞書を適用
        for key in sorted_keys:
            # ワードバウンダリ等を考慮しつつ置換（大文字小文字無視）
            pattern = re.compile(re.escape(key), re.IGNORECASE)
            text = pattern.sub(pronunciation_dict[key], text)
            
        fixed_lines.append((speaker_id, text))
        
    return fixed_lines

# === 4. VOICEVOXによる音声合成 ===
def check_voicevox_ready():
    print("Checking if VOICEVOX is running...")
    for _ in range(10):
        try:
            res = requests.get(f"{VOICEVOX_URL}/speakers", timeout=2)
            if res.status_code == 200:
                print("VOICEVOX is ready!")
                return True
        except requests.exceptions.RequestException:
            pass
        print("Waiting for VOICEVOX to start...")
        time.sleep(3)
    return False

def generate_voice(speaker_id, text, output_path):
    # 1. 音声合成用のクエリを作成
    query_payload = {
        "text": text,
        "speaker": speaker_id
    }
    
    # クエリ作成
    res_query = requests.post(f"{VOICEVOX_URL}/audio_query", params=query_payload, timeout=30)
    if res_query.status_code != 200:
        print(f"Error: audio_query failed for text: {text}")
        return False
        
    query_json = res_query.json()
    
    # 2. 音声波形データを生成
    res_synthesis = requests.post(
        f"{VOICEVOX_URL}/synthesis",
        params={"speaker": speaker_id},
        json=query_json,
        timeout=60
    )
    
    if res_synthesis.status_code != 200:
        print(f"Error: synthesis failed for text: {text}")
        return False
        
    # 保存
    with open(output_path, "wb") as f:
        f.write(res_synthesis.content)
    return True

# === 5. 音声の結合とRSS更新 ===
def build_podcast_audio(fixed_script):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    temp_files = []
    
    print(f"Synthesizing {len(fixed_script)} lines of dialog...")
    
    for idx, (speaker_id, text) in enumerate(fixed_script):
        # 空白行はスキップ
        if not text.strip():
            continue
            
        speaker = SPEAKER_ZUNDAMON if speaker_id == "1" else SPEAKER_METAN
        temp_file = f"temp_{idx}.wav"
        
        # 音声生成（最大3回リトライ）
        success = False
        for attempt in range(3):
            if generate_voice(speaker, text, temp_file):
                success = True
                break
            print(f"Retrying line {idx} (attempt {attempt+1}/3)...")
            time.sleep(2)
            
        if success:
            temp_files.append(temp_file)
        else:
            print(f"Failed to generate voice for: {text}")

    if not temp_files:
        print("No audio segments were successfully generated.")
        return None

    # 音声ファイルの結合
    print("Combining audio segments...")
    combined = AudioSegment.empty()
    
    for temp_file in temp_files:
        segment = AudioSegment.from_wav(temp_file)
        # セリフの合間に0.5秒の無音を挟む
        silence = AudioSegment.silent(duration=500)
        combined += segment + silence
        
    # 一時ファイルの削除
    for temp_file in temp_files:
        try:
            os.remove(temp_file)
        except Exception as e:
            print(f"Error deleting temp file {temp_file}: {e}")

    # MP3としてエクスポート
    today_str = datetime.now().strftime("%Y%m%d")
    output_filename = f"podcast_{today_str}.mp3"
    output_path = os.path.join(OUTPUT_DIR, output_filename)
    
    combined.export(output_path, format="mp3", bitrate="128k")
    print(f"Exported final podcast to {output_path}")
    return output_filename

def update_podcast_rss(audio_filename, articles_used):
    print("Updating podcast.xml (RSS feed)...")
    
    # 既存のRSSファイルをロード、なければ新規作成用の骨組みを作成
    if os.path.exists(RSS_FILE):
        try:
            tree = ET.parse(RSS_FILE)
            root = tree.getroot()
            channel = root.find("channel")
        except Exception as e:
            print(f"Failed to parse existing RSS, creating new. Error: {e}")
            root, channel = create_new_rss_structure()
    else:
        root, channel = create_new_rss_structure()

    # 今日のエピソードメタデータ
    today = datetime.now(timezone.utc)
    pub_date_str = today.strftime("%a, %d %b %Y %H:%M:%S +0000")
    
    audio_url = f"https://{os.environ.get('GITHUB_REPOSITORY_OWNER', 'user')}.github.io/{os.environ.get('GITHUB_REPOSITORY_NAME', 'repo')}/podcasts/{audio_filename}"
    audio_path = os.path.join(OUTPUT_DIR, audio_filename)
    file_size = os.path.getsize(audio_path)
    
    # 取り上げたニュースのタイトルをエピソード説明に入れる
    news_titles = "\n".join([f"- {art['title']} ({art['url']})" for art in articles_used])
    description_text = f"毎朝のテックニュースポッドキャストです。\n\n【本日のニュース】\n{news_titles}"

    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = f"テックニュース {today.strftime('%Y年%m月%d日')}"
    ET.SubElement(item, "description").text = description_text
    ET.SubElement(item, "pubDate").text = pub_date_str
    
    # iTunes用のタグ
    ET.SubElement(item, "enclosure", {
        "url": audio_url,
        "length": str(file_size),
        "type": "audio/mpeg"
    })
    ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = audio_filename

    # XMLを整形して保存
    # ElementTreeにはインデントフォーマットがないため簡易書き出し
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ", level=0)
    tree.write(RSS_FILE, encoding="utf-8", xml_declaration=True)
    print("RSS feed updated successfully.")

def create_new_rss_structure():
    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"
    })
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "毎朝テックニュースポッドキャスト"
    ET.SubElement(channel, "link").text = f"https://{os.environ.get('GITHUB_REPOSITORY_OWNER', 'user')}.github.io/{os.environ.get('GITHUB_REPOSITORY_NAME', 'repo')}/"
    ET.SubElement(channel, "description").text = "ずんだもんと四国めたんが毎朝最新のテックニュースをお届けするポッドキャストです。"
    ET.SubElement(channel, "language").text = "ja"
    ET.SubElement(channel, "itunes:author").text = "Tech Podcast System"
    ET.SubElement(channel, "itunes:summary").text = "ずんだもんと四国めたんによるテックニュース掛け合い番組。"
    
    category = ET.SubElement(channel, "itunes:category", {"text": "Technology"})
    ET.SubElement(category, "itunes:category", {"text": "Tech News"})
    
    return rss, channel

def update_episodes_json(audio_filename, articles_used):
    print("Updating episodes.json for Web UI...")
    episodes_file = "episodes.json"
    episodes = load_json(episodes_file, [])
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    # 重複防止のため、今日のデータがあれば一旦除外して追加
    episodes = [ep for ep in episodes if ep.get("date") != today_str]
    
    new_episode = {
        "date": today_str,
        "audio_file": f"podcasts/{audio_filename}",
        "articles": [
            {
                "title": art["title"],
                "url": art["url"],
                "summary": re.sub(r'<[^>]*>', '', art.get("summary", ""))[:300]
            }
            for art in articles_used
        ]
    }
    
    episodes.insert(0, new_episode) # 最新を一番上に
    save_json(episodes_file, episodes)

def update_history(articles_used):
    history = load_json(HISTORY_FILE, [])
    for art in articles_used:
        history.append({
            "title": art["title"],
            "url": art["url"],
            "fetched_at": art["fetched_at"]
        })
    save_json(HISTORY_FILE, history)

# === メイン制御フロー ===
def main():
    # ニュースの収集と優先度付け
    articles = fetch_news()
    if not articles:
        print("No new articles to process today.")
        return

    # 台本生成（バッチ処理）
    script_lines, articles_used = process_and_combine_scripts(articles)
    if not script_lines:
        print("Failed to generate script.")
        return

    # 発音カタカナ置き換え
    fixed_script = apply_pronunciation_fixes(script_lines)

    # VOICEVOXが起動しているか確認
    if not check_voicevox_ready():
        print("Error: VOICEVOX engine is not running or responsive.")
        sys.exit(1)

    # 音声合成と結合
    audio_filename = build_podcast_audio(fixed_script)
    if not audio_filename:
        print("Failed to build podcast audio.")
        return

    # ポッドキャストRSS更新
    update_podcast_rss(audio_filename, articles_used)

    # Web UI用JSONの更新
    update_episodes_json(audio_filename, articles_used)

    # 配信済み履歴の更新
    update_history(articles_used)
    print("All tasks completed successfully!")

if __name__ == "__main__":
    main()
