import os
import sys
import json
import re
import time
import argparse
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
def fetch_and_pool_news():
    print("Fetching and pooling news feeds...")
    articles = []
    
    # 優先度ルールの読み込み
    priorities = load_json(PRIORITIES_FILE, {"must_include": [], "boost": []})
    must_include = priorities.get("must_include", [])
    boost_words = priorities.get("boost", [])

    # 重複排除履歴（過去7日間）の読み込みとクリーンアップ
    history = load_json(HISTORY_FILE, [])
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat() # 週刊化に合わせて履歴保持を14日間に延長
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
                
                for word in must_include:
                    if word.lower() in text_to_check:
                        score += 100
                
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

    # 既存のプール（未採用記事）をロード
    existing_pool = load_json(CARRYOVER_FILE, [])
    
    # 古いプール記事（例えば14日以上前）を除外
    pool_cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    existing_pool = [art for art in existing_pool if art.get("fetched_at", "") > pool_cutoff]

    # 今回取得分と既存プールを統合
    all_articles = existing_pool + articles
    
    # 重複排除（URLベース）
    unique_articles = {}
    for art in all_articles:
        url = art["url"]
        if url in used_urls:
            continue
        # よりスコアの高い方を残す
        if url not in unique_articles or art["score"] > unique_articles[url]["score"]:
            unique_articles[url] = art

    # スコア順にソートして保存
    pooled_list = sorted(unique_articles.values(), key=lambda x: x["score"], reverse=True)
    save_json(CARRYOVER_FILE, pooled_list)
    print(f"Total articles pooled in carryover.json: {len(pooled_list)}")

    # --- 今日の日次ニュース記事リストを daily_news.json に書き出す ---
    update_daily_news(articles)


def update_daily_news(new_articles):
    """今日取得したニュース記事をdaily_news.jsonに追記・更新する。"""
    DAILY_NEWS_FILE = "daily_news.json"
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    daily_data = load_json(DAILY_NEWS_FILE, [])

    # 今日の日付エントリを探す（なければ新規作成）
    today_entry = next((d for d in daily_data if d.get("date") == today_str), None)
    if today_entry is None:
        today_entry = {"date": today_str, "articles": []}
        daily_data.insert(0, today_entry)

    # 既存URLと重複しない記事だけ追加
    existing_urls = {a["url"] for a in today_entry["articles"]}
    for art in new_articles:
        if art["url"] not in existing_urls:
            today_entry["articles"].append({
                "title": art["title"],
                "url": art["url"],
                "summary": re.sub(r'<[^>]*>', '', art.get("summary", ""))[:300],
                "score": art.get("score", 0),
                "fetched_at": art.get("fetched_at", "")
            })
            existing_urls.add(art["url"])

    # スコア順に並び替え
    today_entry["articles"].sort(key=lambda x: x.get("score", 0), reverse=True)

    # 30日より古いエントリを削除
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    daily_data = [d for d in daily_data if d.get("date", "") >= cutoff]

    save_json(DAILY_NEWS_FILE, daily_data)
    print(f"daily_news.json updated: {len(today_entry['articles'])} articles for {today_str}.")

# === 2. Gemini API を使用した台本生成 ===
def generate_script_with_gemini(articles_batch, is_first, is_last):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY environment variable is not set.")
        sys.exit(1)

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={api_key}"
    
    articles_text = ""
    for i, art in enumerate(articles_batch):
        clean_summary = re.sub(r'<[^>]*>', '', art['summary'])
        articles_text += f"【記事{i+1}】\nタイトル: {art['title']}\n概要: {clean_summary[:300]}\n\n"

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
            "番組全体の台本を作成します。週刊ポッドキャストですので、冒頭に「今週のテックニュースまとめです」などの短いオープニング挨拶を入れ、"
            "ニュースを紹介し、最後は「それではまた来週！」というエンディングトークで締めくくってください。"
        )
    elif is_first:
        flow_instruction = (
            "番組の「第1パート」の台本です。冒頭に「今週のテックニュースまとめです」などのオープニング挨拶を入れ、"
            "ニュースの解説を行ってください。挨拶が終わったらすぐに本題に入ってください。"
            "バッチ処理で後続のニュースが続くため、全体の締めくくりや「それではまた来週」といった終わりの挨拶は絶対に含めないでください。"
        )
    elif is_last:
        flow_instruction = (
            "番組の「最終パート」の台本です。冒頭の挨拶は絶対に含めず、すぐにニュースの解説から始めてください。"
            "ニュースの解説が終わったら、最後に「今週も良い一週間を！」「それではまた来週！」といったエンディングトークで締めくくってください。"
        )
    else:
        flow_instruction = (
            "番組の「中間パート」の台本です。冒ラーの挨拶（おはようございます等）も、"
            "最後の締めくくり（それではまた来週等）も絶対に含めないでください。純粋にニュース記事の紹介・解説の掛け合いだけを作成してください。"
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

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=120)
            response.raise_for_status()
            res_json = response.json()
            script_text = res_json['candidates'][0]['content']['parts'][0]['text']
            return script_text
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            print(f"Gemini API timeout or connection error (attempt {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(5)
            else:
                print("Gemini API failed due to timeout after max retries.")
        except Exception as e:
            print(f"Gemini API call failed with error: {e}")
            break
            
    return ""

def process_and_combine_scripts(articles):
    # 週刊まとめなので、少し多めの最大8本を取り上げる
    target_articles = articles[:8]
    carryover_articles = articles[8:]
    save_json(CARRYOVER_FILE, carryover_articles)
    print(f"Selected {len(target_articles)} articles for this week's podcast. Remaining {len(carryover_articles)} kept in pool.")

    batch_size = 3
    batches = [target_articles[i:i + batch_size] for i in range(0, len(target_articles), batch_size)]

    combined_script = []
    
    for idx, batch in enumerate(batches):
        is_first = (idx == 0)
        is_last = (idx == len(batches) - 1)
        print(f"Generating script for batch {idx+1}/{len(batches)}...")
        
        script = generate_script_with_gemini(batch, is_first, is_last)
        if script:
            for line in script.split("\n"):
                line = line.strip()
                if line.startswith("1:") or line.startswith("2:"):
                    combined_script.append(line)
        time.sleep(2)
        
    return combined_script, target_articles

# === 3. カタカナ読み替え (発音修正) ===
def apply_pronunciation_fixes(script_lines):
    pronunciation_dict = load_json(PRONUNCIATION_FILE, {})
    fixed_lines = []
    sorted_keys = sorted(pronunciation_dict.keys(), key=len, reverse=True)
    
    for line in script_lines:
        match = re.match(r"^([12]):\s*(.*)$", line)
        if not match:
            continue
        speaker_id = match.group(1)
        text = match.group(2)
        
        for key in sorted_keys:
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
    query_payload = {
        "text": text,
        "speaker": speaker_id
    }
    res_query = requests.post(f"{VOICEVOX_URL}/audio_query", params=query_payload, timeout=30)
    if res_query.status_code != 200:
        print(f"Error: audio_query failed for text: {text}")
        return False
        
    query_json = res_query.json()
    res_synthesis = requests.post(
        f"{VOICEVOX_URL}/synthesis",
        params={"speaker": speaker_id},
        json=query_json,
        timeout=60
    )
    if res_synthesis.status_code != 200:
        print(f"Error: synthesis failed for text: {text}")
        return False
        
    with open(output_path, "wb") as f:
        f.write(res_synthesis.content)
    return True

# === 5. 音声の結合とRSS更新 ===
def build_podcast_audio(fixed_script):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    temp_files = []
    print(f"Synthesizing {len(fixed_script)} lines of dialog...")
    
    for idx, (speaker_id, text) in enumerate(fixed_script):
        if not text.strip():
            continue
            
        speaker = SPEAKER_ZUNDAMON if speaker_id == "1" else SPEAKER_METAN
        temp_file = f"temp_{idx}.wav"
        
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

    print("Combining audio segments...")
    combined = AudioSegment.empty()
    for temp_file in temp_files:
        segment = AudioSegment.from_wav(temp_file)
        silence = AudioSegment.silent(duration=500)
        combined += segment + silence
        
    for temp_file in temp_files:
        try:
            os.remove(temp_file)
        except Exception as e:
            print(f"Error deleting temp file {temp_file}: {e}")

    today_str = datetime.now().strftime("%Y%m%d")
    output_filename = f"podcast_{today_str}.mp3"
    output_path = os.path.join(OUTPUT_DIR, output_filename)
    combined.export(output_path, format="mp3", bitrate="128k")
    print(f"Exported final podcast to {output_path}")
    return output_filename

def update_podcast_rss(audio_filename, articles_used):
    print("Updating podcast.xml (RSS feed)...")
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

    today = datetime.now(timezone.utc)
    pub_date_str = today.strftime("%a, %d %b %Y %H:%M:%S +0000")
    audio_url = f"https://{os.environ.get('GITHUB_REPOSITORY_OWNER', 'user')}.github.io/{os.environ.get('GITHUB_REPOSITORY_NAME', 'repo')}/podcasts/{audio_filename}"
    audio_path = os.path.join(OUTPUT_DIR, audio_filename)
    file_size = os.path.getsize(audio_path)
    
    news_titles = "\n".join([f"- {art['title']} ({art['url']})" for art in articles_used])
    description_text = f"今週のテックニュースまとめポッドキャストです。\n\n【今週のニュース一覧】\n{news_titles}"

    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = f"週刊テックニュース {today.strftime('%Y年%m月%d日')}号"
    ET.SubElement(item, "description").text = description_text
    ET.SubElement(item, "pubDate").text = pub_date_str
    
    ET.SubElement(item, "enclosure", {
        "url": audio_url,
        "length": str(file_size),
        "type": "audio/mpeg"
    })
    ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = audio_filename

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
    ET.SubElement(channel, "title").text = "週刊テックニュースポッドキャスト"
    ET.SubElement(channel, "link").text = f"https://{os.environ.get('GITHUB_REPOSITORY_OWNER', 'user')}.github.io/{os.environ.get('GITHUB_REPOSITORY_NAME', 'repo')}/"
    ET.SubElement(channel, "description").text = "ずんだもんと四国めたんが1週間の最新テックニュースをまとめてお届けするポッドキャストです。"
    ET.SubElement(channel, "language").text = "ja"
    ET.SubElement(channel, "itunes:author").text = "Tech Podcast System"
    ET.SubElement(channel, "itunes:summary").text = "ずんだもんと四国めたんによる週刊テックニュース解説番組。"
    
    category = ET.SubElement(channel, "itunes:category", {"text": "Technology"})
    ET.SubElement(category, "itunes:category", {"text": "Tech News"})
    return rss, channel

def update_episodes_json(audio_filename, articles_used):
    print("Updating episodes.json for Web UI...")
    episodes_file = "episodes.json"
    episodes = load_json(episodes_file, [])
    today_str = datetime.now().strftime("%Y-%m-%d")
    
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
    episodes.insert(0, new_episode)
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
    parser = argparse.ArgumentParser(description="Podcast Generator")
    parser.add_argument(
        "--mode",
        choices=["fetch", "build"],
        default="build",
        help="fetch: ニュース収集のみ行いプールへ蓄積 / build: プールからポッドキャストを作成して配信"
    )
    args = parser.parse_args()

    if args.mode == "fetch":
        # 毎日実行されるニュース収集・蓄積処理
        fetch_and_pool_news()
        print("Fetch mode completed successfully.")
        
    elif args.mode == "build":
        # 週に1回実行されるポッドキャスト生成処理
        pooled_articles = load_json(CARRYOVER_FILE, [])
        if not pooled_articles:
            print("No articles in pool. Running fetch first...")
            fetch_and_pool_news()
            pooled_articles = load_json(CARRYOVER_FILE, [])
            
        if not pooled_articles:
            print("No articles available to build podcast.")
            return

        # 台本生成（バッチ処理）
        script_lines, articles_used = process_and_combine_scripts(pooled_articles)
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
        print("Build mode completed successfully!")

if __name__ == "__main__":
    main()
