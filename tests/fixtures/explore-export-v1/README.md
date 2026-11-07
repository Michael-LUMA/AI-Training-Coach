# 固定非敏感导出回归样本（2026-10-18）

records.json 包含3条完全合成记录，共用同一张320×240纯色JPEG（1829字节）。没有真实照片、个人信息、模型推理或OCR读取；A7/B4等文字只是人工构造的字段，不能据此评价识别准确率。

1. describe：转义标签、emoji与截断提示。
2. read_text：非空原始文字、JSON换行/标签转义。
3. read_text：空原始文字/no_text=true，状态说明不是识别原文。

review.html由当前static/explore.js的exportHTML生成，不手工伪造页面。tests/test_archive.js检验readArchive→exportHTML与此文件逐字符相等。Python验证器另核对JPEG真实解码、尺寸/hash/字节数、可见字段/原始OCR、固定CSP与被动资源结构。验证器不是通用HTML安全审计或完整归档schema认证，也不能认证AI/手工元数据真伪。

复现（工作目录 E:\luma测试\.code\ai_companion）：
```powershell
node --test tests/test_archive.js tests/test_explore.js
python -X utf8 -m unittest discover -s tests -p test_explore_export.py -v
python -X utf8 tools/verify_explore_export.py --archive tests/fixtures/explore-export-v1/records.json --html tests/fixtures/explore-export-v1/review.html
```
如用--report，必须给一个尚不存在的新文件；工具拒绝覆盖。无参数模式是历史两张实拍验收，会写历史报告，请勿为本轮重复运行。
