# 照片整理 (Photo Organizer)

<img src="assets/icon.png" width="96" alt="icon">

[한국어](README.md) · [English](README.en.md) · [日本語](README.ja.md)

> **无需联网即可使用。**
> **照片不会上传到任何地方。**
> **每次运行都可以撤销。**

把手机、相机、KakaoTalk 中堆积的照片和视频按拍摄日期整理到文件夹（`2024/2024-03-15/`），把 iPhone 的 HEIC 转为 JPG，并挑出重复照片的 Windows 程序。

![整理前后的文件夹](docs/before_after.png)

![程序界面](docs/screenshot.png)

## 下载

- **程序**：从 [Releases](https://github.com/microhan1/photo-organizer/releases) 下载 `photo-organizer.exe`，双击运行，无需安装。（exe 未签名，如果 SmartScreen 提示，请选择“更多信息 → 仍要运行”。）
- **从源码运行**（Python 3.12）：

```bash
pip install -r requirements.txt
python main.py
```

## 使用方法

1. 把照片文件夹拖到窗口中。
2. 在表格中确认拍摄日期、依据和新路径。日期不对的照片，双击拍摄日期即可修改（只改变文件夹，文件中的 EXIF 保持不变）。
3. 按 **运行**。不满意就 **撤销**。

按“运行”之前不会改动任何文件。程序先保存记录（`organize_log.json`），无法保存时不移动任何文件。撤销会把移动的文件、转换生成的 JPG、删除的空文件夹全部恢复原样。

| 状态 | 含义 |
| --- | --- |
| 正常 | 移到新路径 |
| 转换 | 移动并把 HEIC 转为 JPG |
| 无日期 | 找不到日期。默认留在原处（可在设置中改为移到“无日期”文件夹） |
| 重复 | 移到 `_重复` 文件夹（不删除） |
| 冲突 | 已有同名文件，加上 ` (2)` |
| 无变化 | 已在正确位置。再次运行时全部是这个状态 |

## 日期从哪里读取

从上往下，使用第一个找到的。表格的“依据”列会显示来源。

| 顺序 | 来源 | 说明 |
| --- | --- | --- |
| 1 | EXIF `DateTimeOriginal` | JPG、HEIC、TIFF、RAW（DNG、CR2、NEF、ARW）、PNG。拍摄地的当地时间（不做时区换算） |
| 2 | EXIF `DateTimeDigitized`、`DateTime` | |
| 3 | 视频信息 | MP4、MOV、3GP。iPhone 视频优先使用本地时间 `creationdate` |
| 4 | 文件名 | 见下表 |
| 5 | 配对文件 | HEIC↔JPG、RAW↔JPG、照片↔实况照片 `.mov` |
| 6 | 文件修改时间 | 选项，默认关闭。依据显示“修改时间（不确定）” |

相机时钟被重置的日期（2000-01-01 00:00 等）标为“拍摄信息（可疑）”，文件名中有日期时优先使用文件名。未来的日期和 1990 年以前的日期不被采用（范围可在设置中修改）。

## 能识别的文件名

| 来源 | 例子 |
| --- | --- |
| KakaoTalk | `KakaoTalk_20240315_123456789.jpg`、`KakaoTalk_Photo_2024-03-15-12-34-56.jpeg` |
| 三星 | `20240315_123456.jpg`、`20240315_123456(1).jpg` |
| Google 相册・Pixel | `PXL_20240315_123456789.jpg`、`IMG_20240315_123456.jpg` |
| WhatsApp | `IMG-20240315-WA0001.jpg` |
| 截图 | `Screenshot_20240315_123456.png`、`스크린샷 2024-03-15 123456.png`、`Screen Shot 2024-03-15 at 12.34.56.png` |
| NAVER Band・LINE | `band_20240315.jpg`、`LINE_ALBUM_…_20240315…` |
| 其他 | 文件名中任意位置的 `2024-03-15`、`20240315`、`2024.03.15` |

`IMG_1234.jpg`、`DSC_0001.jpg` 这类没有日期的文件名，从 EXIF 或配对文件中查找。其他规则可以在 设置 → 日期 中用正则表达式添加。

## 文件夹规则

可选 `{yyyy}/{yyyy-mm-dd}`（默认）、`{yyyy}/{yyyy-mm}`、`{yyyy-mm-dd}`、`{yyyy}/{source}/{yyyy-mm-dd}`（`2024/KakaoTalk/2024-03-15/`），也可以自己输入。占位符：`{yyyy}` `{mm}` `{dd}` `{yyyy-mm}` `{yyyy-mm-dd}` `{source}`（KakaoTalk、截图、聊天软件、相机、其他） `{ext}`。无论哪种语言，日期格式都是 `2024-03-15`。

同名（仅扩展名不同）的配对文件 `.mov`（实况照片）、`.aae`（iPhone 编辑信息）、`.xmp`、RAW 原片会跟随照片一起移动。其中一个失败时两者都留在原处。

## 只转换 HEIC

在文件夹规则列表最下方选择 **不整理 — 仅转换 HEIC** 并运行：文件夹保持不变，只在每个 HEIC 旁边生成同名 JPG。无需安装 Windows 编解码器，也不上传照片。

- 拍摄日期、方向、GPS、相机信息（EXIF）和颜色配置文件（iPhone Display P3）原样保留。照片会真正转正保存，避免在某些查看器中横躺。
- 默认质量 92（设置中可选 80~100）。原始 HEIC 保留在原处，或移到 `_原始HEIC` 文件夹，不会删除。
- 实况照片的 `.mov` 不转换。连拍 HEIC 只转换第一张。

命令行：`python main.py D:\照片 --heic-only`

## 查找重复

| 步骤 | 方法 | 默认 |
| --- | --- | --- |
| 完全相同 | 内容相同（SHA-1）。保留一个，其余移到 `_重复` | 开 |
| 同一张照片的不同格式 | HEIC 与其 JPG、RAW 与其 JPG | 视为配对，不算重复 |
| 相似照片 | 感知哈希（pHash），也比较旋转。只显示分组，由你决定保留哪张 | 关（开始前显示预计时间） |

KakaoTalk 压缩过的副本、分辨率较小的副本、重新保存的副本会标为“建议作为重复”。连拍不给建议：哪张最好只有你知道。

## 命令行

```bash
python main.py D:\照片 --dry-run
python main.py D:\照片 --pattern "{yyyy}/{yyyy-mm}" --dest E:\整理 --copy --convert-heic --dedupe
python main.py D:\照片 --undo
```

选项：`--pattern`、`--dest`、`--copy`、`--convert-heic`、`--heic-only`、`--dedupe`、`--similar`、`--use-mtime`、`--include-nodate`、`--dry-run`、`--undo`、`--lang ko|en|zh-CN|ja`

## 不做的事

- 不编辑照片（不裁剪、不调色）。转换时只把方向转正。
- 不做人脸识别、地点识别、按人物分类。
- 不连接任何云服务（Google 相册、iCloud、OneDrive）。OneDrive・iCloud 中“仅在线”的文件不打开、直接跳过（打开会开始下载）。
- 不修改 EXIF。在表格中修改日期只改变文件夹。
- 不在界面上显示 GPS 坐标（转换时只原样复制）。
- 不直接删除文件。重复文件移到 `_重复` 文件夹（可在设置中改为回收站）。

## 许可证

MIT。使用的库：Pillow、pillow-heif（libheif）、piexif、sv-ttk、tkinterdnd2。
