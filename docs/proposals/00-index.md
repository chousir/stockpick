# Proposals — 審查後改善規劃書索引（2026-06-26）

> 本資料夾是 **2026-06-26 資深架構＋量化審查**衍生的改善規劃書。
> 與 `docs/00`–`docs/17`（原始設計規格）區隔：那些是「系統怎麼長成現在這樣」，
> 這些是「接下來怎麼改更好」。每份規劃書依 milestone 體例（目標／成功標準／
> 可動檔案範圍／驗收指令）撰寫，**供後續一次做一個 milestone 逐步執行**。

## 文件清單（依主題分組——同性質的改動收在同一份）

| 規劃書 | 主題 | 對應審查發現 | 風險 | 預期效益 |
|---|---|---|---|---|
| [01-performance-and-techdebt.md](01-performance-and-techdebt.md) | 效能與技術債修復 | §1#1,#4,#5,#6 / §5#5,#6 | 低 | 隨週數惡化的效能止血、寫法統一 |
| [02-data-resilience-and-expansion.md](02-data-resilience-and-expansion.md) | 資料源韌性與擴充 | §1#2 / §2.2,§2.3 / §4#3,#4,#5 | 中 | 降低 Goodinfo 單點故障、補最痛的籌碼/財報缺口 |
| [03-quant-validation-loop.md](03-quant-validation-loop.md) | 量化驗證閉環 | §4#1,#2,#6 | 中 | 證明 edge、補市場層剎車與組合風控 |
| [04-architecture-refactor-and-slimming.md](04-architecture-refactor-and-slimming.md) | 架構重構與精簡 | §1#7,#8 / §5#1,#2,#3,#4,#7,#8 | 中 | 巨石模組拆解、研究軌收斂、文件對齊 |

### 第二波（2026-07-01 分析師實戰回饋審查・人設角色自評）

> 起因：分析師檢視實際週報時，挑戰「壽險/航空轉弱卻因 20 日累積被評太高」，
> 進而攤出整套決策流程的結構性弱點。第一波是「資深架構＋量化」外部審查，
> 這波是**分析師用真實資金的角度自評**——四個真問題＋一組報告品質收尾。
> **注意**：撰寫前已對程式現況查證，修正了原始批評中兩處失準（見各份「含對原始批評的修正」）：
> #3 軌跡資料其實存在（8,849 檔日線）、#2 近端窗欄位其實已有且次產業已有 flow_turn。

| 規劃書 | 主題 | 對應審查發現 | 風險 | 預期效益 |
|---|---|---|---|---|
| [05-pick-outcome-loop.md](05-pick-outcome-loop.md) | 選股結果回饋閉環 | §真問題#1（最致命） | 中 | **精選 pick 層**命中率＋α/β 拆解、翻轉解剖——ROI 最高 |
| [06-near-term-flow-primacy.md](06-near-term-flow-primacy.md) | 近端籌碼提為主判 | §真問題#2 | 中 | 拆窗自動化、消除「前段買兇近端已停」被高估 |
| [07-price-trajectory-signal.md](07-price-trajectory-signal.md) | 多日價量軌跡訊號 | §真問題#3 | 低 | 從既有日線蒸餾「止穩 vs 破線」，不新增資料源 |
| [08-active-vs-passive-flow.md](08-active-vs-passive-flow.md) | 主動 vs 被動法人流 | §真問題#4 | 中 | proxy 標「被動配置疑慮」，把大型金控淨額 conviction 打折 |
| [09-report-quality.md](09-report-quality.md) | 報告層品質 | §次要（肥/空方/一致性） | 低 | 分層瘦身、空方失效前哨、產物完整性檢查 |

## 建議執行順序（由低風險、立即見效 → 高價值、需設計）

```
階段一（止血・低風險）   01 效能技術債  →  04-A6 文件漂移修正（順手）
階段二（補洞・中風險）   02 資料韌性與擴充（D1 Goodinfo 韌性優先）
階段三（證明・高價值）   03 量化驗證閉環（V1 策略回測閉環＝最高 ROI）
階段四（瘦身・收斂）     04 架構重構與精簡（趁前三階段熟悉程式碼後再動）
```

> 為何 04 的重構放最後：拆 `cli.py`、收斂研究軌會大面積動程式碼，建議等
> 01–03 把該補的補完、該驗的驗完，需求穩定後再重構，避免重構到一半又要改需求。

第二波建議順序（第一波 01–04 已全收官，可直接接第二波）：

```
先決（前置）    05-PO1 精選 pick 持久化（後面命中率/翻轉都靠它）
最高 ROI       05-PO2/PO3 命中率×α/β＋翻轉解剖（回答「這套有沒有預測力」）
可平行         06-NF1 近端旗標系統化 ｜ 07-TR1 軌跡訊號 ｜ 08-AP1 被動疑慮 proxy
              （三者皆「先揭露、零 edge 假設」，是否升排序主判交回測 06-NF2/07-TR2/08-AP2 定）
收尾           09 報告品質（瘦身/空方硬化/產物檢查）
```

> 為何 05 先：06/07/08 加的近端/軌跡/proxy 欄「有沒有用」都要靠 05 的回測驗證，
> 且 05-PO3 翻轉解剖是把旺宏「核心→陷阱」那類案例變可學習的關鍵。**不先建閉環，
> 後面全是沒有裁判的球賽。**

## Milestone 命名建議

沿用 `docs/08-milestones.md` 慣例，新 milestone 以 `M-R*` 前綴（R＝Review-driven）：
- `M-R-Perf1` … 對應 01 的 Phase P1
- `M-R-Data1` … 對應 02 的 D1
- `M-R-Val1`  … 對應 03 的 V1
- `M-R-Refac1`… 對應 04 的 A1
- 第二波（分析師回饋）以 `M-F*` 前綴（F＝Field-feedback）：
  `M-F-PO1…PO3`（05）、`M-F-NF1/NF2`（06）、`M-F-TR1/TR2`（07）、`M-F-AP1/AP2`（08）、`M-F-RQ1…RQ3`（09）。

每個 milestone 完成後跑驗收指令、列完成清單、停下等使用者說「下一個」（CLAUDE.md Part 2.1）。

## 共通工程約束（所有規劃書都遵守）

- Polars（不用 pandas）、httpx、loguru、type hints、參數進 `config/settings.yaml` 不寫死。
- 每個改動都要能追溯到某條審查發現（Surgical Changes）。
- 新功能至少一個 happy-path test；解析類用 `tests/fixtures/` 離線檔、不打網。
- 觸及 `goodinfo` 模組者，先回 `docs/02-data-sources.md` 確認合規規則沒變（CLAUDE.md 2.4）。
- 不破壞既有 `make week GROUP=defg` 主流程與 509 個現存測試。
</content>
</invoke>
