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

## 建議執行順序（由低風險、立即見效 → 高價值、需設計）

```
階段一（止血・低風險）   01 效能技術債  →  04-A6 文件漂移修正（順手）
階段二（補洞・中風險）   02 資料韌性與擴充（D1 Goodinfo 韌性優先）
階段三（證明・高價值）   03 量化驗證閉環（V1 策略回測閉環＝最高 ROI）
階段四（瘦身・收斂）     04 架構重構與精簡（趁前三階段熟悉程式碼後再動）
```

> 為何 04 的重構放最後：拆 `cli.py`、收斂研究軌會大面積動程式碼，建議等
> 01–03 把該補的補完、該驗的驗完，需求穩定後再重構，避免重構到一半又要改需求。

## Milestone 命名建議

沿用 `docs/08-milestones.md` 慣例，新 milestone 以 `M-R*` 前綴（R＝Review-driven）：
- `M-R-Perf1` … 對應 01 的 Phase P1
- `M-R-Data1` … 對應 02 的 D1
- `M-R-Val1`  … 對應 03 的 V1
- `M-R-Refac1`… 對應 04 的 A1

每個 milestone 完成後跑驗收指令、列完成清單、停下等使用者說「下一個」（CLAUDE.md Part 2.1）。

## 共通工程約束（所有規劃書都遵守）

- Polars（不用 pandas）、httpx、loguru、type hints、參數進 `config/settings.yaml` 不寫死。
- 每個改動都要能追溯到某條審查發現（Surgical Changes）。
- 新功能至少一個 happy-path test；解析類用 `tests/fixtures/` 離線檔、不打網。
- 觸及 `goodinfo` 模組者，先回 `docs/02-data-sources.md` 確認合規規則沒變（CLAUDE.md 2.4）。
- 不破壞既有 `make week GROUP=defg` 主流程與 509 個現存測試。
</content>
</invoke>
