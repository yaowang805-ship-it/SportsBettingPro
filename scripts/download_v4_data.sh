#!/bin/bash
# V4.5 数据下载脚本 — VPN 正常时执行
# 下载网球 ATP/WTA + NFL 赔率数据
set -e
DEST="/Users/wangyao/SportsBettingPro/data/pinnacle_historical"
cd "$DEST"

echo "=== 1/3: Tennis ATP 2021-2024 ==="
for year in 2021 2022 2023 2024; do
    url="https://tennis-data.co.uk/${year}/${year}.zip"
    echo "  Downloading $url..."
    curl -sL -o "tennis_atp_${year}.zip" "$url" && \
        unzip -o "tennis_atp_${year}.zip" -d "tennis_atp_${year}" && \
        echo "  OK" || echo "  FAIL"
done

echo "=== 2/3: Tennis WTA 2021-2024 ==="
for year in 2021 2022 2023 2024; do
    url="https://tennis-data.co.uk/${year}w/${year}w.zip"
    echo "  Downloading $url..."
    curl -sL -o "tennis_wta_${year}.zip" "$url" && \
        unzip -o "tennis_wta_${year}.zip" -d "tennis_wta_${year}" && \
        echo "  OK" || echo "  FAIL"
done

echo "=== 3/3: Kaggle NHL (already done) ==="
echo "  Skipped — nhl_nhl_data_plus.csv already downloaded"

echo ""
echo "Done! Next step: python3 scripts/compute_tennis_v4.py"
