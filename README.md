# 苦中找乐 · 干净 RSS

公开订阅地址：

<https://icytear-svg.github.io/kuzhongzhaole-clean-rss/feed.xml>

这个静态 Feed：

- 从《苦中找乐》的小宇宙 RSS 读取节目身份、发布日期和音频 enclosure。
- 从公开单集页恢复完整 shownotes、图片和时间戳。
- 移除小宇宙添加的导流文案以及指向小宇宙节目页的链接。
- 保留原 GUID、发布日期、音频地址、音频长度和 MIME 类型。

GitHub Actions 每 6 小时检查一次更新并重新部署，不需要常驻服务器。

## 本地生成

```bash
python -m pip install -r requirements.txt
python build_clean_rss.py \
  --output-dir site \
  --cache-dir clean_feed/cache \
  --public-base-url https://icytear-svg.github.io/kuzhongzhaole-clean-rss
```

生成结果包括 `site/feed.xml`、逐期 shownotes、清单和校验报告。

音频文件仍由原始托管方提供；这个仓库只托管页面与 RSS 元数据。
