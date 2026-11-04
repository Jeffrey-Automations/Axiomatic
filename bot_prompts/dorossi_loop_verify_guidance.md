

[驗證守則] 瀏覽器或 driver 改動先執行 `py -3 axiomatic/verify_browser.py`，以拋棄式 profile 驗證單一瀏覽器入口。完整登入與導航用 `--full`，隔離產圖再加 `--generate`；不碰正式佇列與登入態。單一入口透過函式庫呼叫 `set_driver(install_driver=False)`，driver 交給 Selenium Manager 解析。先查證實際來源與版本，再判斷限制。正式批次在跑時會讓位：SKIP（exit 3）表示這次未驗到，稍後再試；FAIL（exit 1）才是失敗。
