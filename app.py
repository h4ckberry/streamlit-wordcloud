import os  # フォントファイルの存在確認に使用
import json
import html
import re
import streamlit as st
from pathlib import Path
from urllib.request import urlopen
from janome.tokenizer import Tokenizer
from wordcloud import WordCloud
import matplotlib.pyplot as plt

st.set_page_config(page_title="カスタマイズ可能なワードクラウド（日本語対応）", layout="wide")

# ---- Slack JSON 抽出ユーティリティ（先に定義しておく） ----
def clean_slack_text(text: str) -> str:
    """Slack特有の表現をプレーンテキストに整形する。

    - HTMLエスケープ解除（&gt; など）
    - メンションやユーザーグループ（<@U...>, <!subteam^...|@group> など）を除去
    - リンク表現（<url|label> や <url>）は label があればlabel、なければ空文字に置換
    """
    if not text:
        return ""
    s = html.unescape(text)
    # <url|label> → label に、<url> → 空に
    s = re.sub(r"<https?://[^>|]+\|([^>]+)>", r"\1", s)
    s = re.sub(r"<https?://[^>]+>", "", s)
    # <!subteam^...|@grp> 等 → 表示名（あれば）に、それ以外の <! ... > は除去
    s = re.sub(r"<![^>|]+\|([^>]+)>", r"\1", s)
    s = re.sub(r"<![^>]+>", "", s)
    # <@U123ABC> などのユーザーメンションは除去
    s = re.sub(r"<@[^>]+>", "", s)
    # <#C123|channel> → channel に（保険）
    s = re.sub(r"<#([^>|]+)\|([^>]+)>", r"\2", s)
    # コードブロックや余計な制御文字の簡易整形
    return s.replace("\r", "").strip()


def _extract_from_rich_elements(elements) -> list[str]:
    texts: list[str] = []
    for el in elements or []:
        et = el.get("type")
        if et in ("rich_text_section", "rich_text_quote", "rich_text_preformatted"):
            texts.extend(_extract_from_rich_elements(el.get("elements", [])))
        elif et == "text":
            t = el.get("text", "")
            if t:
                texts.append(t)
        elif et == "link":
            # label 優先、なければ空
            t = el.get("text") or ""
            if t:
                texts.append(t)
        # user / usergroup / channel などは解決できないので無視
    return texts


def extract_text_from_blocks(blocks) -> list[str]:
    out: list[str] = []
    for b in blocks or []:
        bt = b.get("type")
        if bt == "section" and isinstance(b.get("text"), dict):
            out.append(b["text"].get("text", ""))
        elif bt == "rich_text":
            out.extend(_extract_from_rich_elements(b.get("elements", [])))
    return [clean_slack_text(t) for t in out if isinstance(t, str) and t.strip()]


def extract_text_from_slack_export(data) -> str:
    """Slackエクスポート（メッセージ配列）のJSONからテキストを抽出して結合する。"""
    texts: list[str] = []
    if isinstance(data, dict):
        # チャンネル毎に { messages: [...] } の形式の場合に備える
        items = data.get("messages") or data.get("items") or []
    else:
        items = data
    if not isinstance(items, list):
        return ""
    for item in items:
        if not isinstance(item, dict):
            continue
        if "text" in item and isinstance(item["text"], str):
            texts.append(clean_slack_text(item["text"]))
        # blocks（RichTextなど）
        if "blocks" in item:
            texts.extend(extract_text_from_blocks(item.get("blocks")))
        # attachments配下
        if "attachments" in item and isinstance(item["attachments"], list):
            for att in item["attachments"]:
                if isinstance(att, dict):
                    if isinstance(att.get("text"), str):
                        texts.append(clean_slack_text(att["text"]))
                    if "blocks" in att:
                        texts.extend(extract_text_from_blocks(att.get("blocks")))
                    # Botの unfurl 等で message.blocks が入るケース
                    msg = att.get("message")
                    if isinstance(msg, dict) and "blocks" in msg:
                        texts.extend(extract_text_from_blocks(msg.get("blocks")))
    # 空行を整理して返却
    return "\n".join([t for t in texts if t.strip()])

# Streamlitアプリのタイトル
st.title("カスタマイズ可能なワードクラウドジェネレーター（日本語対応）")

# 入力ソースの選択
input_mode = st.radio(
    "入力ソースを選択してください",
    ["テキスト入力", "Slack JSONアップロード"],
    horizontal=True,
)

# 入力テキストのプレースホルダー
user_input = ""
slack_text_preview = ""

if input_mode == "テキスト入力":
    user_input = st.text_area(
        "ワードクラウドを生成するテキストを入力してください",
        "Streamlitは、機械学習とデータサイエンスのためのオープンソースのアプリフレームワークです。"
    )
else:
    uploaded = st.file_uploader("SlackエクスポートJSON（メッセージ配列）をアップロード", type=["json"])
    if uploaded is not None:
        try:
            data = json.load(uploaded)
            slack_text_preview = st.text_area(
                "抽出テキスト（必要なら編集してください）",
                value="\n".join([t for t in extract_text_from_slack_export(data).splitlines() if t.strip()])[:20000],
                height=200,
            )
            user_input = slack_text_preview
        except Exception as e:
            st.error(f"JSONの読み込みに失敗しました: {e}")

# 除外する単語の入力
exclude_input = st.text_input(
    "除外する単語を入力してください（カンマ区切り）",
    value="的, こと, 性"
)

# 除外単語をリストに変換
exclude_words = [word.strip() for word in exclude_input.split(',') if word.strip()]

# 品詞のオプション
pos_options = [
    '名詞',    # Noun
    '動詞',    # Verb
    '形容詞',  # Adjective
    '副詞',    # Adverb
    '助詞',    # Particle
    '助動詞',  # Auxiliary verb
    '連体詞',  # Adnominal adjective
    '接続詞',  # Conjunction
    '感動詞',  # Interjection
    '記号',    # Symbol
    'その他'   # Other
]

# 品詞選択のマルチセレクトウィジェット
selected_pos = st.multiselect(
    "ワードクラウドに含める品詞を選択してください",
    options=pos_options,
    default=['名詞']  # デフォルト選択
)

# ワードクラウド画像の幅入力
width = st.number_input(
    "ワードクラウドの幅（ピクセル）",
    min_value=100,
    max_value=10000,
    value=1280,
    step=1
)

# ワードクラウド画像の高さ入力
height = st.number_input(
    "ワードクラウドの高さ（ピクセル）",
    min_value=100,
    max_value=10000,
    value=670,
    step=1
)

# 背景色の選択
background_color = st.color_picker("背景色を選択してください", "#ffffff")

# フォント（UI と 画像生成）の設定
# 1) UI用のWebフォント適用（任意）
use_google_font_ui = st.checkbox("UIに Google Fonts (Noto Sans JP) を適用する", value=False)
if use_google_font_ui:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@100..900&display=swap');
        html, body, [class*="css"]  { font-family: 'Noto Sans JP', sans-serif; }
        </style>
        """,
        unsafe_allow_html=True,
    )

# 2) WordCloud用フォントの選択（TTF/OTFファイルが必要）
default_font_path = "NotoSansJP-VariableFont_wght.ttf"
font_download_url = st.text_input(
    "WordCloud用フォントURL（TTF/OTF、空欄ならローカルのフォントを使用）",
    value="",
    placeholder="例: https://example.com/NotoSansJP-VariableFont_wght.ttf",
)

def _download_font_to_cache(url: str) -> str | None:
    try:
        cache_dir = Path(".cache/fonts")
        cache_dir.mkdir(parents=True, exist_ok=True)
        filename = url.split("?")[0].split("/")[-1] or "downloaded-font.ttf"
        dest = cache_dir / filename
        with urlopen(url) as resp, open(dest, "wb") as f:
            f.write(resp.read())
        return str(dest)
    except Exception as e:
        st.error(f"フォントのダウンロードに失敗しました: {e}")
        return None

font_path = default_font_path
if font_download_url.strip():
    maybe = _download_font_to_cache(font_download_url.strip())
    if maybe:
        font_path = maybe

# フォントファイルの存在確認
if not os.path.exists(font_path):
    st.error(
        f"指定されたフォントが見つかりません。フォントパスを確認してください: {font_path}"
    )
    st.stop()


def tokenize_japanese(text, selected_pos, exclude_words=None):
    """
    日本語テキストを形態素解析し、選択した品詞のみを含む単語の文字列を返す。

    Args:
        text (str): 解析対象の日本語テキスト。
        selected_pos (list): 含める品詞のリスト。
        exclude_words (list, optional): 除外する単語のリスト。

    Returns:
        str: スペースで区切られた単語の文字列。
    """
    tokenizer = Tokenizer()
    tokens = tokenizer.tokenize(text)
    if exclude_words is None:
        exclude_words = []
    words = ' '.join([
        (token.base_form if token.base_form != '*' else token.surface)
        for token in tokens
        if token.part_of_speech.split(',')[0] in selected_pos
        and (token.base_form if token.base_form != '*' else token.surface) not in exclude_words
    ])
    return words


def generate_wordcloud(text, width, height, background_color, font_path, selected_pos, exclude_words=None):
    """
    トークン化された日本語テキストからワードクラウドを生成する。

    Args:
        text (str): ワードクラウド生成元の日本語テキスト。
        width (int): ワードクラウド画像の幅。
        height (int): ワードクラウド画像の高さ。
        background_color (str): ワードクラウドの背景色。
        font_path (str): フォントファイルへのパス。
        selected_pos (list): 含める品詞のリスト。
        exclude_words (list, optional): 除外する単語のリスト。

    Returns:
        WordCloud: 生成されたWordCloudオブジェクト。
    """
    words = tokenize_japanese(text, selected_pos, exclude_words)
    wordcloud = WordCloud(
        font_path=font_path,
        width=width,
        height=height,
        background_color=background_color,
        collocations=False,
    ).generate(words)
    return wordcloud


# ワードクラウド生成ボタン（Slack対応版: Wordcloud_slackver）
if st.button("Wordcloud_slackver を生成"):
    if not (user_input or "").strip():
        st.error("ワードクラウドを生成するテキストを入力してください。")
    elif not selected_pos:
        st.error("少なくとも1つの品詞を選択してください。")
    else:
        try:
            wc = generate_wordcloud(
                user_input, width, height, background_color,
                font_path, selected_pos, exclude_words
            )

            # ワードクラウドの描画
            fig, ax = plt.subplots(figsize=(width/128, height/128))  # おおよそdpi=128相当
            ax.imshow(wc, interpolation='bilinear')
            ax.axis("off")

            # Streamlit上にワードクラウドを表示
            st.pyplot(fig)
        except Exception as e:
            st.error(f"エラーが発生しました: {e}")
