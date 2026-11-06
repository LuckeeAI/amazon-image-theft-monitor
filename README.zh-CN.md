# Amazon Image Theft Monitor（亚马逊盗图监控）

自动找出 Temu、eBay、Walmart、TikTok Shop 和各类独立站上盗用你亚马逊产品图的链接，保存证据，并生成侵权投诉信和 Buy Box 申诉草稿。

[English](README.md) · **简体中文**

![License: MIT](https://img.shields.io/badge/license-MIT-15803d) ![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-1f2937) ![Claude Code skill](https://img.shields.io/badge/Claude%20Code-skill-0e7490)

既可以当 Python 命令行工具用，也可以作为 Claude Code（或其他读取 `SKILL.md` 的 AI agent）的 skill 使用。由一位亚马逊卖家为自己的店铺开发，[Luckee](https://luckee.ai/?utm_source=github&utm_medium=readme&utm_campaign=amazon-image-theft-monitor&utm_content=intro-zh) 整理后开源。

## 解决什么问题

亚马逊的比价机制发现同款商品在别处卖得更便宜时，可能会拿掉你的 Featured Offer（购物车 / Buy Box）。麻烦的是，这个"别处"有时是盗用了你产品图、卖便宜仿品的 Temu 或 eBay 链接。亚马逊按图片匹配，看到更低的价格，就把你的购物车拿掉了。

你没降价，对方卖的也不是你的产品，但后果是实打实的：

- 没了购物车，自己 listing 的销量大幅下滑
- Sponsored Products 广告只投放给购物车持有者，广告也跟着停止展示
- 卖家支持要求你先提供证据，才会处理申诉

这个工具就是帮你拿到这些证据。

## 功能

1. **以图搜图**：用 Google Cloud Vision Web Detection 搜索你的官方产品图
2. **先过滤**：自己的网站、亚马逊和授权渠道在下载前就排除，不花额度
3. **相似度校验**：对每张可疑图片做 pHash/dHash 感知哈希比对，可选再用 Vertex AI Vision Warehouse 校验
4. **保存证据**：链接、匹配类型、哈希距离、图片副本，以及 CSV 和 JSONL 报告
5. **生成文书**：按平台分别生成投诉信（Temu、eBay VeRO、Walmart、TikTok Shop，其他网站生成 DMCA 通知），另附一份恢复购物车的亚马逊卖家支持 case

工具不会替你提交任何东西。草稿需要你自己检查后提交。

## 快速开始

需要 Python 3.10+。自动以图搜图还需要一个开通了 Cloud Vision API 的 Google Cloud 服务账号（见[配置指南](docs/google-cloud-setup.md)）。没有 Google Cloud？见[自己粘贴链接](#没有-google-cloud自己粘贴链接)。

```bash
git clone https://github.com/LuckeeAI/amazon-image-theft-monitor.git
cd amazon-image-theft-monitor
pip install -r requirements.txt

cp templates/products.yaml templates/monitor_config.yaml .
mkdir -p images
# 把官方产品图放进 images/，然后编辑 products.yaml（品牌、品名、SKU、ASIN、图片路径）
# 和 monitor_config.yaml 里的域名名单

export GOOGLE_APPLICATION_CREDENTIALS="/secure/path/service-account.json"

# 1. 试运行：只看能搜到什么，不下载
python scripts/product_image_abuse_monitor.py --config monitor_config.yaml --products products.yaml --dry-run

# 2. 正式运行：下载可疑图片、打分、写入 evidence/
python scripts/product_image_abuse_monitor.py --config monitor_config.yaml --products products.yaml

# 3. 为高度疑似盗图的链接生成投诉草稿
python scripts/product_image_abuse_monitor.py --config monitor_config.yaml --products products.yaml --generate-complaints
```

### 没有 Google Cloud？自己粘贴链接

用 [Google Lens](https://lens.google.com)、Bing 图片搜索或 TinEye 搜一下你的主图，把可疑结果填进一个 CSV（格式见 `templates/candidates.csv`）：

```csv
product_id,page_url,image_url,note
SKU-001,https://www.temu.com/goods.html?goods_id=...,https://img.kwcdn.com/...jpg,Google Lens 找到
SKU-001,https://www.ebay.com/itm/...,,image_url 可以留空，工具会尝试抓取页面主图
```

```bash
python scripts/product_image_abuse_monitor.py --config monitor_config.yaml --products products.yaml --candidates-file links.csv
python scripts/product_image_abuse_monitor.py --config monitor_config.yaml --products products.yaml --generate-complaints
```

工具会下载每张图片、和你的原图比对，把链接分成"高度疑似"和"需要人工确认"两类，再生成投诉草稿。不需要 Google 账号或 API key。

### 作为 Claude Code skill 使用

```bash
git clone https://github.com/LuckeeAI/amazon-image-theft-monitor.git ~/.claude/skills/amazon-image-theft-monitor
```

然后直接对 Claude 说："我 B0XXXXXXX 的购物车没了，帮我查查是不是有人在 Temu 上用了我的图。"它会带你走完配置、试运行、正式运行和生成投诉草稿的全过程。

## 输出内容

```
evidence/
├── report.csv              本次所有候选链接及判定
├── probable_abuse.csv      高度疑似盗图，需要处理
├── needs_review.csv        可能盗图，需要人工确认
├── page_matches.csv        页面里有匹配但没有直接图片链接
├── images/<product_id>/    可疑图片副本
└── complaints/<product_id>/
    ├── SKU-001_temu_complaint.txt
    ├── SKU-001_ebay_complaint.txt
    ├── SKU-001_dmca_notice_other_sites.txt
    └── SKU-001_amazon_case.txt
```

亚马逊申诉 case 和 Temu 投诉信的示例见 [`examples/`](examples/)。

判定规则：

| 判定 | 条件 |
|---|---|
| `probable_abuse` | 在非白名单域名上完全匹配，或部分匹配且哈希距离很小 |
| `needs_review` | 部分匹配或视觉相似 |
| `not_match` | 本地和 Warehouse 校验相似度都低 |
| `ignored` | 自有、授权或噪音域名 |

## 购物车拿回来之后，记得检查广告

购物车丢失期间，Sponsored Products 广告是停止展示的。如果设置了自动调价规则，它们可能已经根据曝光和销量的下滑做了调整。购物车恢复后，建议尽快检查出价、预算和被暂停的关键词。

[Luckee](https://luckee.ai/features/amazon-ads?utm_source=github&utm_medium=readme&utm_campaign=amazon-image-theft-monitor&utm_content=after-buybox-zh) 是你的 AI 亚马逊广告专家，记得你每一个产品的情况。它会检查你的广告活动，说明发生了什么变化，并给出出价和预算调整建议，经你确认后才会执行。[免费试用 →](https://luckee.ai/features/amazon-ads?utm_source=github&utm_medium=readme&utm_campaign=amazon-image-theft-monitor&utm_content=after-buybox-cta-zh)

## 常见问题

**会自动提交投诉吗？**
不会，只生成草稿。你需要填写自己的信息、附上截图，然后在各平台自行提交。

**运行要花多少钱？**
代码免费。Google 按图片请求次数收取 Cloud Vision Web Detection 费用，具体以 Google 当前价格为准。域名过滤和每个产品的调用上限能让请求次数保持在较低水平。

**一定要配置 Vision Warehouse 吗？**
不用。不配置时使用本地 pHash/dHash 比对，大多数店铺够用。

**匹配结果能直接作为侵权证明吗？**
不能，它只是待核实的线索。请逐条确认，并且只针对你拥有或获授权维权的图片提交投诉。

**可以定时运行吗？**
可以，每周跑一次 cron 就很合适。已检查过的链接会缓存在 `evidence/seen_urls.json` 里。见 [SKILL.md](SKILL.md#step-7--schedule-monitoring)。

**支持飞书吗？**
支持（可选）：`--feishu-sync` 会把结果写入飞书多维表格。见 [SKILL.md](SKILL.md#optional-feishu-lark-base-sync)。

## 致谢与许可

由 [Luckee](https://luckee.ai/?utm_source=github&utm_medium=readme&utm_campaign=amazon-image-theft-monitor&utm_content=credits-zh) 开发和维护。

代码采用 [MIT 许可证](LICENSE)。本项目与 Amazon、Temu、eBay、Walmart、TikTok、Google 均无关联。
