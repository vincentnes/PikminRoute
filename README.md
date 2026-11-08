# iPhone 路線模擬器 v0.2

Windows 11 x64 版。用 USB 接 iPhone，在電腦輸入路線，讓 iPhone 回報的位置沿著路線移動。執行檔已內建執行環境和 pymobiledevice3 11.50.0，雙擊即可啟動。

## 首次啟動

1. 在 Windows 準備 [Apple Devices](https://support.apple.com/zh-tw/108643)。用 USB 資料線接上一支 iPhone，解鎖並在手機上選「信任」。確認 Apple Devices 看得到手機，然後關閉 iAnyGo 等正在使用定位模擬的工具。
2. 雙擊 `PikminRoute.exe`，或從同一資料夾執行 `Start.bat`。
3. 按「檢查連線」。顯示「定位服務可用」後，就可以測試路線。

## 如果顯示開發服務尚未提供

在 iPhone 的「設定 → 隱私權與安全性 → 開發者模式」開啟開發者模式，依手機提示重新啟動並確認。然後保持手機解鎖，在解壓縮的資料夾開啟命令提示字元，執行：

```bat
PikminRoute.exe mounter auto-mount --userspace
```

這個步驟在需要時會下載並掛載開發者映像，須有網路。完成後重新按「檢查連線」。若找不到開發者模式、掛載失敗，或 Apple Devices 能認到手機但程式連不上，請記下第一個錯誤訊息。

## 跑一條短路線

按「儲存路線」可把座標與速度存成 JSON 檔，之後可按「載入路線」開啟。

1. 從地圖複製同一條步道上的至少兩個座標，每行貼一個，格式為 `緯度, 經度`。程式按順序在相鄰座標間連線，轉彎處要另外加點。
2. 速度預設 **15 km/h**，可以手動修改。這是測試起點。準備好後按「開始路線」。
3. 第一次可以先用 Apple 地圖看藍點是否沿路移動。若你要在遊戲中驗證，開始種花仍在 iPhone 的《Pikmin Bloom》內操作。
4. 按「停止並恢復」，或等路線走完。程式會送出恢復定位指令。用 Apple 地圖確認藍點回到實際位置，再拔線。如果中途斷線，接回同一支手機，按「恢復定位」。如果仍未恢復，可重新啟動 iPhone 再確認。

《Pikmin Bloom》將偽造 GPS 視為違規，有帳號停權風險。[官方政策](https://scopelyexplore.helpshift.com/hc/zh-hant/11-pikmin-bloom/faq/2211-three-strike-discipline-policy/)

已檢查路線計算和模擬流程。Windows 桌機、iOS 27.0.1 實機連線以及遊戲內種花仍待實測。[連線套件說明](https://doronz88.github.io/pymobiledevice3/latest/guides/python-api/)
