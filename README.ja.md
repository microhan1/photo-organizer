# 写真整理 (Photo Organizer)

<img src="assets/icon.png" width="96" alt="icon">

[한국어](README.md) · [English](README.en.md) · [中文](README.zh-CN.md)

> **インターネット接続なしで動作します。**
> **写真はどこにもアップロードされません。**
> **実行はいつでも元に戻せます。**

スマホ・カメラ・KakaoTalk から溜まった写真と動画を撮影日ごとのフォルダー（`2024/2024-03-15/`）に整理し、iPhone の HEIC を JPG に変換し、重複を取り分ける Windows 用プログラムです。

![整理前と整理後のフォルダー](docs/before_after.png)

![プログラム画面](docs/screenshot.png)

## ダウンロード

- **プログラム**: [Releases](https://github.com/microhan1/photo-organizer/releases) から `photo-organizer.exe` をダウンロードしてダブルクリック。インストール不要です。（署名のない exe のため SmartScreen の警告が出たら「詳細情報 → 実行」）
- **ソースから実行**（Python 3.12）:

```bash
pip install -r requirements.txt
python main.py
```

## 使い方

1. 写真フォルダーをウィンドウにドロップします。
2. 表で撮影日・根拠・新しいパスを確認します。日付が違う写真は撮影日をダブルクリックして直します（フォルダーだけが変わり、ファイル内の EXIF はそのまま）。
3. **実行** を押します。気に入らなければ **元に戻す**。

実行するまで何も変わりません。記録（`organize_log.json`）を先に保存し、保存できなければファイルは一つも移動しません。元に戻すと、移動したファイル、変換で作った JPG、削除した空フォルダーがすべて元どおりになります。

| 状態 | 意味 |
| --- | --- |
| 正常 | 新しいパスへ移動 |
| 変換 | 移動しながら HEIC → JPG |
| 日付なし | 日付が見つからない。既定ではそのまま（設定で「日付なし」フォルダーへ） |
| 重複 | `_重複` フォルダーへ（削除はしない） |
| 衝突 | 同じ名前があるので ` (2)` を付ける |
| 変更なし | すでに正しい場所。もう一度実行するとすべてこれ |

## 日付をどこから読むか

上から順に、最初に見つかったものを使います。表の「根拠」列に出どころが表示されます。

| 順序 | 出どころ | 備考 |
| --- | --- | --- |
| 1 | EXIF `DateTimeOriginal` | JPG・HEIC・TIFF・RAW（DNG・CR2・NEF・ARW）・PNG。撮影地の時刻のまま（タイムゾーン変換なし） |
| 2 | EXIF `DateTimeDigitized`、`DateTime` | |
| 3 | 動画情報 | MP4・MOV・3GP。iPhone の動画はローカル時刻 `creationdate` を優先 |
| 4 | ファイル名 | 下の表 |
| 5 | ペアのファイル | HEIC↔JPG、RAW↔JPG、写真↔Live Photos の `.mov` |
| 6 | ファイルの更新日時 | オプション、既定はオフ。根拠に「更新日時（不確か）」 |

カメラの時計がリセットされた日付（2000-01-01 00:00 など）は「撮影情報（疑わしい）」と表示し、ファイル名に日付があればそちらを使います。未来の日付と 1990 年より前の日付は信用しません（範囲は設定で変更可）。

## 認識するファイル名

| 出どころ | 例 |
| --- | --- |
| KakaoTalk | `KakaoTalk_20240315_123456789.jpg`、`KakaoTalk_Photo_2024-03-15-12-34-56.jpeg` |
| Samsung | `20240315_123456.jpg`、`20240315_123456(1).jpg` |
| Google フォト・Pixel | `PXL_20240315_123456789.jpg`、`IMG_20240315_123456.jpg` |
| WhatsApp | `IMG-20240315-WA0001.jpg` |
| スクリーンショット | `Screenshot_20240315_123456.png`、`스크린샷 2024-03-15 123456.png`、`Screen Shot 2024-03-15 at 12.34.56.png` |
| NAVER Band・LINE | `band_20240315.jpg`、`LINE_ALBUM_…_20240315…` |
| その他 | ファイル名のどこかにある `2024-03-15`、`20240315`、`2024.03.15` |

`IMG_1234.jpg`、`DSC_0001.jpg` のように日付のない名前は、EXIF やペアのファイルから探します。ほかのパターンは 設定 → 日付 で正規表現として追加できます。

## フォルダー規則

`{yyyy}/{yyyy-mm-dd}`（既定）、`{yyyy}/{yyyy-mm}`、`{yyyy-mm-dd}`、`{yyyy}/{source}/{yyyy-mm-dd}`（`2024/KakaoTalk/2024-03-15/`）から選ぶか、自分で入力します。プレースホルダー: `{yyyy}` `{mm}` `{dd}` `{yyyy-mm}` `{yyyy-mm-dd}` `{source}`（KakaoTalk・スクリーンショット・メッセンジャー・カメラ・その他） `{ext}`。日付の形式は言語にかかわらず `2024-03-15` です。

同じ名前で拡張子だけ違うペアのファイル `.mov`（Live Photos）、`.aae`（iPhone の編集情報）、`.xmp`、RAW 原本は写真と一緒に移動します。片方が失敗したら両方そのままにします。

## HEIC の変換だけしたいとき

フォルダー規則の一覧のいちばん下にある **整理しない — HEIC 変換のみ** を選んで実行すると、フォルダーはそのままで、各 HEIC の隣に同じ名前の JPG だけを作ります。Windows のコーデックのインストールは不要で、写真をどこにもアップロードしません。

- 撮影日・向き・GPS・カメラ情報（EXIF）とカラープロファイル（iPhone の Display P3）をそのまま移します。向きのタグを無視するビューアーでも横倒しにならないよう、写真を実際に回転して保存します。
- 品質は既定 92（設定で 80〜100）。元の HEIC はそのまま残すか `_元HEIC` フォルダーへ移します。削除はしません。
- Live Photos の `.mov` は変換しません。連写の HEIC は 1 枚目だけ変換します。

コマンドライン: `python main.py D:\写真 --heic-only`

## 重複を探す

| 段階 | 方法 | 既定 |
| --- | --- | --- |
| 完全に同じ | 内容が同じファイル（SHA-1）。1 つ残して `_重複` へ | オン |
| 同じ写真の別形式 | HEIC とその JPG、RAW とその JPG | ペアとみなし、重複にしない |
| 似た写真 | 知覚ハッシュ（pHash）、回転も比較。グループを表示するだけで、残すものは人が選ぶ | オフ（始める前に所要時間の目安を表示） |

KakaoTalk で圧縮された写真、解像度の小さいコピー、保存し直したコピーは「重複の候補」と表示します。連写のように、どれが良いかは人にしか分からないものには候補を出しません。

## コマンドライン

```bash
python main.py D:\写真 --dry-run
python main.py D:\写真 --pattern "{yyyy}/{yyyy-mm}" --dest E:\整理 --copy --convert-heic --dedupe
python main.py D:\写真 --undo
```

オプション: `--pattern`、`--dest`、`--copy`、`--convert-heic`、`--heic-only`、`--dedupe`、`--similar`、`--use-mtime`、`--include-nodate`、`--dry-run`、`--undo`、`--lang ko|en|zh-CN|ja`

## しないこと

- 写真を編集しません（トリミング・補正なし）。変換時に向きを直すだけです。
- 顔認識・場所の認識・人物ごとの分類はしません。
- クラウド（Google フォト・iCloud・OneDrive）に接続しません。OneDrive・iCloud の「オンライン専用」ファイルは開かずにスキップします（開くとダウンロードが始まるため）。
- EXIF を書き換えません。表で日付を直してもフォルダーが変わるだけです。
- GPS 座標を画面に表示しません（変換時にそのまま移すだけ）。
- ファイルをすぐには削除しません。重複は `_重複` フォルダーへ（設定でごみ箱も選べます）。

## シリーズ

- しおりツール: [音楽フォルダ整理](https://github.com/microhan1/music-folder-organizer) · [音楽情報を埋める](https://github.com/microhan1/music-tag-filler)
- [しおりライブラリ（Chaekgalpi Library）](https://chaekgalpi.co.kr/tools/photoorganizer?utm_source=github&utm_medium=referral&utm_campaign=tool_cta&utm_content=photoorganizer) — 読んだ本と読書記録を残すウェブサービス（韓国語のみ）

## ライセンス

MIT。使用ライブラリ: Pillow、pillow-heif（libheif）、piexif、sv-ttk、tkinterdnd2。
